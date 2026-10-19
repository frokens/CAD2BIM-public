"""Allow one Component catalog to publish Task- and User-scoped entries.

Revision ID: 20260811_0052
Revises: 20260811_0051
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260811_0052"
down_revision: str | None = "20260811_0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "component_catalog_events",
        sa.Column("principal_id", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "component_catalog_events",
        sa.Column("catalog_scope_key", sa.String(length=80), nullable=True),
    )
    op.execute(
        "UPDATE component_catalog_events "
        "SET catalog_scope_key = task_id "
        "WHERE catalog_scope_key IS NULL"
    )
    op.alter_column(
        "component_catalog_events",
        "catalog_scope_key",
        existing_type=sa.String(length=80),
        nullable=False,
    )
    op.drop_constraint(
        op.f("ck_component_catalog_events_scope_valid"),
        "component_catalog_events",
        type_="check",
    )
    op.drop_constraint(
        "uq_component_catalog_events_task_revision",
        "component_catalog_events",
        type_="unique",
    )
    op.drop_constraint(
        "uq_component_catalog_events_task_idempotency",
        "component_catalog_events",
        type_="unique",
    )
    op.drop_index(
        "ix_component_catalog_events_owner_task_resource_revision",
        table_name="component_catalog_events",
    )
    op.create_foreign_key(
        "fk_component_catalog_events_tenant_principal",
        "component_catalog_events",
        "tenant_principals",
        ["tenant_id", "principal_id"],
        ["tenant_id", "principal_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_component_catalog_events_scope_valid"),
        "component_catalog_events",
        "((scope = 'task' AND principal_id IS NULL "
        "AND catalog_scope_key = task_id) OR "
        "(scope = 'user' AND principal_id IS NOT NULL "
        "AND catalog_scope_key = principal_id))",
    )
    op.create_unique_constraint(
        "uq_component_catalog_events_scope_revision",
        "component_catalog_events",
        ["owner_id", "scope", "catalog_scope_key", "catalog_revision"],
    )
    op.create_unique_constraint(
        "uq_component_catalog_events_scope_idempotency",
        "component_catalog_events",
        ["owner_id", "scope", "catalog_scope_key", "idempotency_key"],
    )
    op.create_index(
        "ix_component_catalog_events_owner_scope_resource_revision",
        "component_catalog_events",
        [
            "owner_id",
            "scope",
            "catalog_scope_key",
            "resource_ref",
            "catalog_revision",
        ],
    )


def downgrade() -> None:
    # User-scoped rows cannot be represented by the previous Task-only schema.
    op.execute("DELETE FROM component_catalog_events WHERE scope = 'user'")
    op.drop_index(
        "ix_component_catalog_events_owner_scope_resource_revision",
        table_name="component_catalog_events",
    )
    op.drop_constraint(
        "uq_component_catalog_events_scope_idempotency",
        "component_catalog_events",
        type_="unique",
    )
    op.drop_constraint(
        "uq_component_catalog_events_scope_revision",
        "component_catalog_events",
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_component_catalog_events_scope_valid"),
        "component_catalog_events",
        type_="check",
    )
    op.drop_constraint(
        "fk_component_catalog_events_tenant_principal",
        "component_catalog_events",
        type_="foreignkey",
    )
    op.create_check_constraint(
        op.f("ck_component_catalog_events_scope_valid"),
        "component_catalog_events",
        "scope = 'task'",
    )
    op.create_unique_constraint(
        "uq_component_catalog_events_task_revision",
        "component_catalog_events",
        ["task_id", "catalog_revision"],
    )
    op.create_unique_constraint(
        "uq_component_catalog_events_task_idempotency",
        "component_catalog_events",
        ["task_id", "idempotency_key"],
    )
    op.create_index(
        "ix_component_catalog_events_owner_task_resource_revision",
        "component_catalog_events",
        ["owner_id", "task_id", "resource_ref", "catalog_revision"],
    )
    op.drop_column("component_catalog_events", "catalog_scope_key")
    op.drop_column("component_catalog_events", "principal_id")
