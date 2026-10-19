"""Add explicit identity, personal tenants, and forced tenant RLS.

Revision ID: 20260719_0018
Revises: 20260719_0017
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260719_0018"
down_revision: str | None = "20260719_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LEGACY_OWNER_TABLES = (
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
TENANT_POLICY_TABLES = LEGACY_OWNER_TABLES + (
    "tenants",
    "tenant_principals",
    "task_tenant_bindings",
)
RUNTIME_ROLES = (
    "cad2bim_api_runtime",
    "cad2bim_runner_runtime",
    "cad2bim_system_curator",
)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _legacy_digest(namespace: str, owner_expression: str) -> str:
    """Return a PostgreSQL SHA-256 expression without requiring pgcrypto."""

    return (
        "encode(sha256(convert_to(" f"'{namespace}:' || {owner_expression}, "
        "'UTF8')), 'hex')"
    )


def _create_runtime_roles() -> None:
    for role in RUNTIME_ROLES:
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                        CREATE ROLE {role}
                            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                            NOINHERIT NOREPLICATION NOBYPASSRLS;
                    ELSE
                        ALTER ROLE {role}
                            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                            NOINHERIT NOREPLICATION NOBYPASSRLS;
                    END IF;
                END
                $$
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    EXECUTE format(
                        'GRANT USAGE ON SCHEMA %I TO {role}',
                        current_schema()
                    );
                END
                $$
                """
            )
        )


def _backfill_legacy_identities() -> None:
    principal_digest = _legacy_digest("legacy-principal", "owner_id")
    tenant_digest = _legacy_digest("legacy-tenant", "owner_id")
    subject_digest = _legacy_digest("legacy-subject", "owner_id")
    op.execute(
        sa.text(
            f"""
            INSERT INTO principals (
                principal_id, issuer, subject, status, legacy_owner_id,
                created_at, updated_at
            )
            SELECT
                'principal_' || {principal_digest},
                'https://cad2bim.local/legacy-owner',
                'legacy-sha256:' || {subject_digest},
                'disabled',
                owner_id,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM users
            ON CONFLICT (legacy_owner_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO tenants (
                tenant_id, kind, status, legacy_owner_id, created_at, updated_at
            )
            SELECT
                'tenant_' || {tenant_digest},
                'personal',
                'active',
                owner_id,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            FROM users
            ON CONFLICT (legacy_owner_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO tenant_principals (
                tenant_id, principal_id, access, created_at
            )
            SELECT tenant.tenant_id, principal.principal_id, 'owner', CURRENT_TIMESTAMP
            FROM users AS legacy_user
            JOIN tenants AS tenant
              ON tenant.legacy_owner_id = legacy_user.owner_id
            JOIN principals AS principal
              ON principal.legacy_owner_id = legacy_user.owner_id
            ON CONFLICT (tenant_id, principal_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO identity_migration_quarantine (
                legacy_owner_id, reason_code, task_count, created_at
            )
            SELECT
                legacy_user.owner_id,
                'legacy.identity_issuer_unprovable',
                count(task.task_id),
                CURRENT_TIMESTAMP
            FROM users AS legacy_user
            LEFT JOIN tasks AS task ON task.owner_id = legacy_user.owner_id
            GROUP BY legacy_user.owner_id
            ON CONFLICT (legacy_owner_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO task_tenant_bindings (
                tenant_id, task_id, principal_id, legacy_owner_id, created_at
            )
            SELECT
                tenant.tenant_id,
                task.task_id,
                principal.principal_id,
                task.owner_id,
                CURRENT_TIMESTAMP
            FROM tasks AS task
            JOIN tenants AS tenant ON tenant.legacy_owner_id = task.owner_id
            JOIN principals AS principal ON principal.legacy_owner_id = task.owner_id
            ON CONFLICT (tenant_id, task_id) DO NOTHING
            """
        )
    )
    for table in LEGACY_OWNER_TABLES:
        op.execute(
            sa.text(
                f"""
                UPDATE {table} AS scoped
                SET tenant_id = tenant.tenant_id
                FROM tenants AS tenant
                WHERE tenant.legacy_owner_id = scoped.owner_id
                  AND scoped.tenant_id IS NULL
                """
            )
        )


def _enable_rls() -> None:
    tenant_expression = (
        "tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '')"
    )
    for table in TENANT_POLICY_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"""
                CREATE POLICY tenant_isolation ON {table}
                USING ({tenant_expression})
                WITH CHECK ({tenant_expression})
                """
            )
        )
    grant_tables = ", ".join(TENANT_POLICY_TABLES)
    for role in ("cad2bim_api_runtime", "cad2bim_runner_runtime"):
        op.execute(
            sa.text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {grant_tables} TO {role}"
            )
        )


def upgrade() -> None:
    op.create_table(
        "principals",
        sa.Column("principal_id", sa.String(length=80), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("legacy_owner_id", sa.String(length=255), nullable=True),
        _updated_at(),
        _created_at(),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name=op.f("ck_principals_status_valid"),
        ),
        sa.PrimaryKeyConstraint("principal_id", name=op.f("pk_principals")),
        sa.UniqueConstraint("issuer", "subject", name="uq_principals_issuer_subject"),
        sa.UniqueConstraint("legacy_owner_id", name="uq_principals_legacy_owner"),
    )
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("legacy_owner_id", sa.String(length=255), nullable=True),
        _updated_at(),
        _created_at(),
        sa.CheckConstraint("kind = 'personal'", name=op.f("ck_tenants_kind_valid")),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')",
            name=op.f("ck_tenants_status_valid"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("legacy_owner_id", name="uq_tenants_legacy_owner"),
    )
    op.create_table(
        "tenant_principals",
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("principal_id", sa.String(length=80), nullable=False),
        sa.Column("access", sa.String(length=32), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "access = 'owner'",
            name=op.f("ck_tenant_principals_access_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.principal_id"],
            name=op.f("fk_tenant_principals_principal_id_principals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_tenant_principals_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "principal_id",
            name=op.f("pk_tenant_principals"),
        ),
    )
    for table in LEGACY_OWNER_TABLES:
        op.add_column(
            table,
            sa.Column("tenant_id", sa.String(length=80), nullable=True),
        )
        op.create_foreign_key(
            op.f(f"fk_{table}_tenant_id_tenants"),
            table,
            "tenants",
            ["tenant_id"],
            ["tenant_id"],
            ondelete="RESTRICT",
        )
        op.create_index(op.f(f"ix_{table}_tenant_id"), table, ["tenant_id"])
    op.create_table(
        "task_tenant_bindings",
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("principal_id", sa.String(length=80), nullable=False),
        sa.Column("legacy_owner_id", sa.String(length=255), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.principal_id"],
            name=op.f("fk_task_tenant_bindings_principal_id_principals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_task_tenant_bindings_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["legacy_owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_task_tenant_bindings_legacy_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "task_id",
            name=op.f("pk_task_tenant_bindings"),
        ),
        sa.UniqueConstraint(
            "legacy_owner_id",
            "task_id",
            name="uq_task_tenant_bindings_legacy_task",
        ),
    )
    op.create_table(
        "identity_migration_quarantine",
        sa.Column("legacy_owner_id", sa.String(length=255), nullable=False),
        sa.Column("reason_code", sa.String(length=255), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint(
            "legacy_owner_id",
            name=op.f("pk_identity_migration_quarantine"),
        ),
    )
    _create_runtime_roles()
    _backfill_legacy_identities()
    _enable_rls()


def downgrade() -> None:
    for role in ("cad2bim_api_runtime", "cad2bim_runner_runtime"):
        op.execute(
            sa.text(
                f"REVOKE ALL PRIVILEGES ON TABLE "
                f"{', '.join(TENANT_POLICY_TABLES)} FROM {role}"
            )
        )
    for table in reversed(TENANT_POLICY_TABLES):
        op.execute(sa.text(f"DROP POLICY tenant_isolation ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
    op.drop_table("identity_migration_quarantine")
    op.drop_table("task_tenant_bindings")
    for table in reversed(LEGACY_OWNER_TABLES):
        op.drop_index(op.f(f"ix_{table}_tenant_id"), table_name=table)
        op.drop_constraint(
            op.f(f"fk_{table}_tenant_id_tenants"),
            table,
            type_="foreignkey",
        )
        op.drop_column(table, "tenant_id")
    op.drop_table("tenant_principals")
    op.drop_table("tenants")
    op.drop_table("principals")
