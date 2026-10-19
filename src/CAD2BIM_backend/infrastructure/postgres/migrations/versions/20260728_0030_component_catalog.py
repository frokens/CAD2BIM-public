"""Add the append-only task Component catalog.

Revision ID: 20260728_0030
Revises: 20260727_0029
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260728_0030"
down_revision: str | None = "20260727_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNTIME_ROLES = ("cad2bim_api_runtime", "cad2bim_runner_runtime")
_TENANT_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '')"
)


def upgrade() -> None:
    # Pre-release hard cut: old rows describe mutable main-session type creation
    # and cannot be converted into immutable Component publication evidence.
    op.execute("DELETE FROM run_component_views")
    op.drop_constraint(
        "fk_run_component_views_owner_task_artifact_version",
        "run_component_views",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_run_component_views_source_artifact_pair"),
        "run_component_views",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_run_component_views_status_valid"),
        "run_component_views",
        type_="check",
    )
    for column_name in (
        "identity_source",
        "ifc_type_guid",
        "source_artifact_id",
        "source_artifact_version_id",
        "source_artifact_version_no",
    ):
        op.drop_column("run_component_views", column_name)
    for column in (
        sa.Column("resource_ref", sa.String(length=500), nullable=False),
        sa.Column("version", sa.String(length=200), nullable=False),
        sa.Column("content_digest", sa.String(length=200), nullable=False),
        sa.Column("catalog_revision", sa.BigInteger(), nullable=False),
        sa.Column("published_artifact_id", sa.String(length=80), nullable=False),
        sa.Column(
            "published_artifact_version_id",
            sa.String(length=80),
            nullable=False,
        ),
        sa.Column("published_artifact_version_no", sa.Integer(), nullable=False),
        sa.Column("receipt_artifact_id", sa.String(length=80), nullable=False),
        sa.Column(
            "receipt_artifact_version_id",
            sa.String(length=80),
            nullable=False,
        ),
        sa.Column("receipt_artifact_version_no", sa.Integer(), nullable=False),
        sa.Column("reused", sa.Boolean(), nullable=False),
    ):
        op.add_column("run_component_views", column)
    op.create_check_constraint(
        op.f("ck_run_component_views_status_valid"),
        "run_component_views",
        "status = 'published'",
    )
    op.create_check_constraint(
        op.f("ck_run_component_views_catalog_revision_positive"),
        "run_component_views",
        "catalog_revision > 0",
    )
    op.create_check_constraint(
        op.f("ck_run_component_views_published_artifact_version_positive"),
        "run_component_views",
        "published_artifact_version_no > 0",
    )
    op.create_check_constraint(
        op.f("ck_run_component_views_receipt_artifact_version_positive"),
        "run_component_views",
        "receipt_artifact_version_no > 0",
    )
    op.create_foreign_key(
        "fk_run_component_views_owner_task_published_artifact_version",
        "run_component_views",
        "artifact_versions",
        [
            "owner_id",
            "task_id",
            "published_artifact_id",
            "published_artifact_version_id",
        ],
        ["owner_id", "task_id", "artifact_id", "version_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_run_component_views_owner_task_receipt_artifact_version",
        "run_component_views",
        "artifact_versions",
        [
            "owner_id",
            "task_id",
            "receipt_artifact_id",
            "receipt_artifact_version_id",
        ],
        ["owner_id", "task_id", "artifact_id", "version_id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "component_catalog_events",
        sa.Column("catalog_entry_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("source_run_id", sa.String(length=80), nullable=True),
        sa.Column("source_stage_key", sa.String(length=255), nullable=True),
        sa.Column("catalog_revision", sa.BigInteger(), nullable=False),
        sa.Column("resource_ref", sa.String(length=500), nullable=False),
        sa.Column("resource_kind", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("version", sa.String(length=200), nullable=False),
        sa.Column("content_digest", sa.String(length=200), nullable=False),
        sa.Column("availability", sa.String(length=32), nullable=False),
        sa.Column("artifact_id", sa.String(length=80), nullable=False),
        sa.Column("artifact_version_id", sa.String(length=80), nullable=False),
        sa.Column("artifact_version_no", sa.Integer(), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("ifc_type_class", sa.String(length=100), nullable=False),
        sa.Column("primitive_kind", sa.String(length=32), nullable=False),
        sa.Column("spec_sha256", sa.String(length=64), nullable=False),
        sa.Column("requirement_id", sa.String(length=80), nullable=False),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "catalog_revision > 0",
            name=op.f("ck_component_catalog_events_catalog_revision_positive"),
        ),
        sa.CheckConstraint(
            "resource_kind = 'component_template'",
            name=op.f("ck_component_catalog_events_resource_kind_valid"),
        ),
        sa.CheckConstraint(
            "scope = 'task'",
            name=op.f("ck_component_catalog_events_scope_valid"),
        ),
        sa.CheckConstraint(
            "availability IN ('active', 'revoked')",
            name=op.f("ck_component_catalog_events_availability_valid"),
        ),
        sa.CheckConstraint(
            "artifact_version_no > 0",
            name=op.f("ck_component_catalog_events_artifact_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_component_catalog_events_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_component_catalog_events_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_component_catalog_events_owner_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "source_run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_component_catalog_events_owner_task_run",
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
            name="fk_component_catalog_events_owner_task_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "catalog_entry_id",
            name=op.f("pk_component_catalog_events"),
        ),
        sa.UniqueConstraint(
            "task_id",
            "catalog_revision",
            name="uq_component_catalog_events_task_revision",
        ),
        sa.UniqueConstraint(
            "task_id",
            "idempotency_key",
            name="uq_component_catalog_events_task_idempotency",
        ),
    )
    op.create_index(
        op.f("ix_component_catalog_events_tenant_id"),
        "component_catalog_events",
        ["tenant_id"],
    )
    op.create_index(
        "ix_component_catalog_events_owner_task_resource_revision",
        "component_catalog_events",
        ["owner_id", "task_id", "resource_ref", "catalog_revision"],
    )
    op.execute("ALTER TABLE component_catalog_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE component_catalog_events FORCE ROW LEVEL SECURITY")
    op.execute(
        sa.text(
            f"""
            CREATE POLICY tenant_isolation ON component_catalog_events
            USING ({_TENANT_EXPRESSION})
            WITH CHECK ({_TENANT_EXPRESSION})
            """
        )
    )
    for role in _RUNTIME_ROLES:
        op.execute(
            sa.text(
                f"GRANT SELECT, INSERT ON TABLE component_catalog_events TO {role}"
            )
        )


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON component_catalog_events")
    op.drop_index(
        "ix_component_catalog_events_owner_task_resource_revision",
        table_name="component_catalog_events",
    )
    op.drop_index(
        op.f("ix_component_catalog_events_tenant_id"),
        table_name="component_catalog_events",
    )
    op.drop_table("component_catalog_events")
    op.execute("DELETE FROM run_component_views")
    op.drop_constraint(
        "fk_run_component_views_owner_task_receipt_artifact_version",
        "run_component_views",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_run_component_views_owner_task_published_artifact_version",
        "run_component_views",
        type_="foreignkey",
    )
    for constraint_name in (
        "ck_run_component_views_receipt_artifact_version_positive",
        "ck_run_component_views_published_artifact_version_positive",
        "ck_run_component_views_catalog_revision_positive",
        "ck_run_component_views_status_valid",
    ):
        op.drop_constraint(
            op.f(constraint_name),
            "run_component_views",
            type_="check",
        )
    for column_name in (
        "reused",
        "receipt_artifact_version_no",
        "receipt_artifact_version_id",
        "receipt_artifact_id",
        "published_artifact_version_no",
        "published_artifact_version_id",
        "published_artifact_id",
        "catalog_revision",
        "content_digest",
        "version",
        "resource_ref",
    ):
        op.drop_column("run_component_views", column_name)
    for column in (
        sa.Column("identity_source", sa.String(length=32), nullable=True),
        sa.Column("ifc_type_guid", sa.String(length=22), nullable=True),
        sa.Column("source_artifact_id", sa.String(length=80), nullable=True),
        sa.Column("source_artifact_version_id", sa.String(length=80), nullable=True),
        sa.Column("source_artifact_version_no", sa.Integer(), nullable=True),
    ):
        op.add_column("run_component_views", column)
    op.create_check_constraint(
        op.f("ck_run_component_views_status_valid"),
        "run_component_views",
        "status IN ('resolved', 'unresolved')",
    )
    op.create_check_constraint(
        op.f("ck_run_component_views_source_artifact_pair"),
        "run_component_views",
        "(source_artifact_id IS NULL) = (source_artifact_version_id IS NULL)",
    )
    op.create_foreign_key(
        "fk_run_component_views_owner_task_artifact_version",
        "run_component_views",
        "artifact_versions",
        [
            "owner_id",
            "task_id",
            "source_artifact_id",
            "source_artifact_version_id",
        ],
        ["owner_id", "task_id", "artifact_id", "version_id"],
        ondelete="RESTRICT",
    )
