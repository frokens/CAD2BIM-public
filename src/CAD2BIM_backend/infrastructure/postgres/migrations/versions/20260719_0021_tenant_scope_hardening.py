"""Make tenant scope mandatory and bind it to Task ownership.

Revision ID: 20260719_0021
Revises: 20260719_0020
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260719_0021"
down_revision: str | None = "20260719_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TENANT_OWNED_TABLES = (
    "users",
    "tasks",
    "runs",
    "checkpoint_projection_receipts",
    "run_stage_business_views",
    "run_artifact_views",
    "run_component_views",
    "run_events",
    "interrupts",
    "feedback",
    "stage_plans",
    "artifacts",
    "artifact_versions",
    "ifc_run_projections",
    "ifc_run_stage_projections",
    "mcp_sessions",
    "mcp_snapshots",
    "checkpoint_links",
    "safe_point_publications",
    "run_resume_operations",
)
TASK_RESOURCE_TABLES = tuple(
    table for table in TENANT_OWNED_TABLES if table not in {"users", "tasks"}
)


def _backfill_resolvable_nulls() -> None:
    op.execute(
        sa.text(
            """
            UPDATE users AS scoped
               SET tenant_id = tenant.tenant_id
              FROM tenants AS tenant
             WHERE scoped.tenant_id IS NULL
               AND tenant.legacy_owner_id = scoped.owner_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE tasks AS scoped
               SET tenant_id = owner_alias.tenant_id
              FROM users AS owner_alias
             WHERE scoped.tenant_id IS NULL
               AND owner_alias.owner_id = scoped.owner_id
            """
        )
    )
    for table in TASK_RESOURCE_TABLES:
        op.execute(
            sa.text(
                f"""
                UPDATE {table} AS scoped
                   SET tenant_id = task.tenant_id
                  FROM tasks AS task
                 WHERE scoped.tenant_id IS NULL
                   AND task.task_id = scoped.task_id
                """
            )
        )


def _raise_if_counted(*, code: str, query: str) -> None:
    op.execute(
        sa.text(
            f"""
            DO $block$
            DECLARE
                violation_count bigint;
            BEGIN
                SELECT count(*) INTO violation_count FROM ({query}) AS violations;
                IF violation_count <> 0 THEN
                    RAISE EXCEPTION
                        'tenant_scope_hardening_failed:{code}:%', violation_count
                        USING ERRCODE = '23514';
                END IF;
            END
            $block$
            """
        )
    )


def _validate_scope() -> None:
    for table in TENANT_OWNED_TABLES:
        _raise_if_counted(
            code=f"{table}.tenant_missing_or_orphaned",
            query=(
                f"SELECT 1 FROM {table} AS scoped "
                "LEFT JOIN tenants AS tenant ON tenant.tenant_id = scoped.tenant_id "
                "WHERE scoped.tenant_id IS NULL OR tenant.tenant_id IS NULL"
            ),
        )

    _raise_if_counted(
        code="tasks.owner_tenant_mismatch",
        query=(
            "SELECT 1 FROM tasks AS scoped "
            "LEFT JOIN users AS owner_alias ON owner_alias.owner_id = scoped.owner_id "
            "WHERE owner_alias.owner_id IS NULL "
            "OR scoped.tenant_id IS DISTINCT FROM owner_alias.tenant_id"
        ),
    )
    for table in TASK_RESOURCE_TABLES:
        _raise_if_counted(
            code=f"{table}.task_tenant_mismatch",
            query=(
                f"SELECT 1 FROM {table} AS scoped "
                "LEFT JOIN tasks AS task ON task.task_id = scoped.task_id "
                "WHERE task.task_id IS NULL "
                "OR scoped.owner_id IS DISTINCT FROM task.owner_id "
                "OR scoped.tenant_id IS DISTINCT FROM task.tenant_id"
            ),
        )
    _raise_if_counted(
        code="task_tenant_bindings.task_tenant_mismatch",
        query=(
            "SELECT 1 FROM task_tenant_bindings AS scoped "
            "LEFT JOIN tasks AS task ON task.task_id = scoped.task_id "
            "WHERE task.task_id IS NULL "
            "OR scoped.tenant_id IS DISTINCT FROM task.tenant_id "
            "OR scoped.legacy_owner_id IS DISTINCT FROM task.owner_id"
        ),
    )
    _raise_if_counted(
        code="identity_migration_quarantine.tenant_scope_unresolved",
        query=(
            "SELECT 1 FROM identity_migration_quarantine "
            "WHERE reason_code LIKE 'tenant.scope.%'"
        ),
    )


def _create_scope_constraints() -> None:
    op.create_unique_constraint(
        "uq_users_owner_tenant",
        "users",
        ["owner_id", "tenant_id"],
    )
    op.create_unique_constraint(
        "uq_tasks_tenant_task",
        "tasks",
        ["tenant_id", "task_id"],
    )
    op.create_foreign_key(
        "fk_tasks_owner_tenant_users",
        "tasks",
        "users",
        ["owner_id", "tenant_id"],
        ["owner_id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_task_tenant_bindings_tenant_task_tasks",
        "task_tenant_bindings",
        "tasks",
        ["tenant_id", "task_id"],
        ["tenant_id", "task_id"],
        ondelete="CASCADE",
    )
    for table in TASK_RESOURCE_TABLES:
        op.create_foreign_key(
            f"fk_{table}_tenant_task_tasks",
            table,
            "tasks",
            ["tenant_id", "task_id"],
            ["tenant_id", "task_id"],
            ondelete="CASCADE",
        )


def upgrade() -> None:
    _backfill_resolvable_nulls()
    _validate_scope()
    for table in TENANT_OWNED_TABLES:
        op.alter_column(
            table,
            "tenant_id",
            existing_type=sa.String(length=80),
            nullable=False,
        )
    _create_scope_constraints()


def downgrade() -> None:
    for table in reversed(TASK_RESOURCE_TABLES):
        op.drop_constraint(
            f"fk_{table}_tenant_task_tasks",
            table,
            type_="foreignkey",
        )
    op.drop_constraint(
        "fk_task_tenant_bindings_tenant_task_tasks",
        "task_tenant_bindings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_tasks_owner_tenant_users",
        "tasks",
        type_="foreignkey",
    )
    op.drop_constraint("uq_tasks_tenant_task", "tasks", type_="unique")
    op.drop_constraint("uq_users_owner_tenant", "users", type_="unique")
    for table in reversed(TENANT_OWNED_TABLES):
        op.alter_column(
            table,
            "tenant_id",
            existing_type=sa.String(length=80),
            nullable=True,
        )
