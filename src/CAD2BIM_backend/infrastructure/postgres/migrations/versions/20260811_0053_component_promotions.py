"""Persist exact Task-to-User Component promotion lineage.

Revision ID: 20260811_0053
Revises: 20260811_0052
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260811_0053"
down_revision: str | None = "20260811_0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNTIME_ROLES = ("cad2bim_api_runtime", "cad2bim_runner_runtime")
_TENANT_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '')"
)


def upgrade() -> None:
    op.create_table(
        "component_promotions",
        sa.Column("promotion_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("principal_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_catalog_entry_id", sa.String(length=80), nullable=False),
        sa.Column("source_definition_id", sa.String(length=80), nullable=False),
        sa.Column("source_component_version_id", sa.String(length=80), nullable=False),
        sa.Column("source_manifest_digest", sa.String(length=200), nullable=False),
        sa.Column("target_catalog_entry_id", sa.String(length=80), nullable=False),
        sa.Column("target_definition_id", sa.String(length=80), nullable=False),
        sa.Column("target_component_version_id", sa.String(length=80), nullable=False),
        sa.Column("target_manifest_digest", sa.String(length=200), nullable=False),
        sa.Column("target_resource_ref", sa.String(length=500), nullable=False),
        sa.Column("policy_digest", sa.String(length=200), nullable=False),
        sa.Column("validation_digest", sa.String(length=200), nullable=False),
        sa.Column("receipt_digest", sa.String(length=200), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.CheckConstraint(
            "status = 'completed'",
            name=op.f("ck_component_promotions_status_valid"),
        ),
        sa.CheckConstraint(
            "source_catalog_entry_id <> target_catalog_entry_id",
            name=op.f("ck_component_promotions_catalog_lineage_distinct"),
        ),
        sa.CheckConstraint(
            "source_definition_id <> target_definition_id",
            name=op.f("ck_component_promotions_definition_lineage_distinct"),
        ),
        sa.CheckConstraint(
            "source_component_version_id <> target_component_version_id",
            name=op.f("ck_component_promotions_version_lineage_distinct"),
        ),
        sa.CheckConstraint(
            "source_manifest_digest = target_manifest_digest",
            name=op.f("ck_component_promotions_immutable_manifest"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_component_promotions_owner_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_component_promotions_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["tenant_principals.tenant_id", "tenant_principals.principal_id"],
            name="fk_component_promotions_tenant_principal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_catalog_entry_id"],
            ["component_catalog_events.catalog_entry_id"],
            name="fk_component_promotions_source_catalog_entry",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_catalog_entry_id"],
            ["component_catalog_events.catalog_entry_id"],
            name="fk_component_promotions_target_catalog_entry",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_component_promotions_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("promotion_id", name=op.f("pk_component_promotions")),
        sa.UniqueConstraint(
            "owner_id",
            "principal_id",
            "idempotency_key",
            name="uq_component_promotions_principal_idempotency",
        ),
    )
    op.create_index(
        op.f("ix_component_promotions_tenant_id"),
        "component_promotions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_component_promotions_owner_principal_created",
        "component_promotions",
        ["owner_id", "principal_id", "created_at"],
    )
    op.execute("ALTER TABLE component_promotions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE component_promotions FORCE ROW LEVEL SECURITY")
    op.execute(
        sa.text(
            f"""
            CREATE POLICY tenant_isolation ON component_promotions
            USING ({_TENANT_EXPRESSION})
            WITH CHECK ({_TENANT_EXPRESSION})
            """
        )
    )
    for role in _RUNTIME_ROLES:
        op.execute(
            sa.text(
                f"GRANT SELECT, INSERT ON TABLE component_promotions TO {role}"
            )
        )


def downgrade() -> None:
    for role in _RUNTIME_ROLES:
        op.execute(
            sa.text(
                f"REVOKE SELECT, INSERT ON TABLE component_promotions FROM {role}"
            )
        )
    op.execute("DROP POLICY tenant_isolation ON component_promotions")
    op.drop_index(
        "ix_component_promotions_owner_principal_created",
        table_name="component_promotions",
    )
    op.drop_index(
        op.f("ix_component_promotions_tenant_id"),
        table_name="component_promotions",
    )
    op.drop_table("component_promotions")
