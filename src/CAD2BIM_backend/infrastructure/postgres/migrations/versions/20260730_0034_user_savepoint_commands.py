"""Add durable user Savepoint commands claimed by the Runner.

Revision ID: 20260730_0034
Revises: 20260730_0033
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0034"
down_revision: str | None = "20260730_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNTIME_ROLES = ("cad2bim_api_runtime", "cad2bim_runner_runtime")
_TENANT_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '')"
)


def upgrade() -> None:
    op.create_table(
        "user_savepoint_commands",
        sa.Column("command_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("author_principal_id", sa.String(length=80), nullable=False),
        sa.Column("client_request_id", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("claimed_runner_id", sa.String(length=255), nullable=True),
        sa.Column("claimed_lease_token_digest", sa.String(length=64), nullable=True),
        sa.Column("claimed_writer_generation", sa.BigInteger(), nullable=True),
        sa.Column("claimed_stage_key", sa.String(length=255), nullable=True),
        sa.Column("claimed_checkpoint_id", sa.String(length=255), nullable=True),
        sa.Column("claimed_checkpoint_ns", sa.String(length=1024), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("savepoint_id", sa.String(length=80), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('requested', 'claimed', 'linked')",
            name=op.f("ck_user_savepoint_commands_status_valid"),
        ),
        sa.CheckConstraint(
            "claimed_writer_generation IS NULL OR claimed_writer_generation > 0",
            name=op.f("ck_user_savepoint_commands_writer_generation_positive"),
        ),
        sa.CheckConstraint(
            "claimed_lease_token_digest IS NULL "
            "OR length(claimed_lease_token_digest) = 64",
            name=op.f("ck_user_savepoint_commands_lease_digest_shape"),
        ),
        sa.CheckConstraint(
            "((status = 'requested' "
            "AND claimed_runner_id IS NULL "
            "AND claimed_lease_token_digest IS NULL "
            "AND claimed_writer_generation IS NULL "
            "AND claimed_stage_key IS NULL "
            "AND claimed_checkpoint_id IS NULL "
            "AND claimed_checkpoint_ns IS NULL "
            "AND claimed_at IS NULL "
            "AND savepoint_id IS NULL "
            "AND linked_at IS NULL) OR "
            "(status = 'claimed' "
            "AND claimed_runner_id IS NOT NULL "
            "AND claimed_lease_token_digest IS NOT NULL "
            "AND claimed_writer_generation IS NOT NULL "
            "AND claimed_stage_key IS NOT NULL "
            "AND claimed_checkpoint_id IS NOT NULL "
            "AND claimed_checkpoint_ns IS NOT NULL "
            "AND claimed_at IS NOT NULL "
            "AND savepoint_id IS NULL "
            "AND linked_at IS NULL) OR "
            "(status = 'linked' "
            "AND claimed_runner_id IS NOT NULL "
            "AND claimed_lease_token_digest IS NOT NULL "
            "AND claimed_writer_generation IS NOT NULL "
            "AND claimed_stage_key IS NOT NULL "
            "AND claimed_checkpoint_id IS NOT NULL "
            "AND claimed_checkpoint_ns IS NOT NULL "
            "AND claimed_at IS NOT NULL "
            "AND savepoint_id IS NOT NULL "
            "AND linked_at IS NOT NULL))",
            name=op.f("ck_user_savepoint_commands_lifecycle_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_user_savepoint_commands_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_user_savepoint_commands_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_user_savepoint_commands_owner_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_user_savepoint_commands_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "author_principal_id"],
            ["tenant_principals.tenant_id", "tenant_principals.principal_id"],
            name="fk_user_savepoint_commands_tenant_author",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "savepoint_id"],
            [
                "checkpoint_links.owner_id",
                "checkpoint_links.task_id",
                "checkpoint_links.savepoint_id",
            ],
            name="fk_user_savepoint_commands_linked_savepoint",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "command_id",
            name=op.f("pk_user_savepoint_commands"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "author_principal_id",
            "client_request_id",
            name="uq_user_savepoint_commands_idempotency",
        ),
    )
    op.create_index(
        "ix_user_savepoint_commands_owner_run_status",
        "user_savepoint_commands",
        ["owner_id", "run_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_user_savepoint_commands_tenant_id",
        "user_savepoint_commands",
        ["tenant_id"],
        unique=False,
    )
    op.execute("ALTER TABLE user_savepoint_commands ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_savepoint_commands FORCE ROW LEVEL SECURITY")
    op.execute(
        sa.text(
            f"""
            CREATE POLICY tenant_isolation ON user_savepoint_commands
            USING ({_TENANT_EXPRESSION})
            WITH CHECK ({_TENANT_EXPRESSION})
            """
        )
    )
    for role in _RUNTIME_ROLES:
        op.execute(
            sa.text(
                "GRANT SELECT, INSERT, UPDATE, DELETE "
                f"ON TABLE user_savepoint_commands TO {role}"
            )
        )


def downgrade() -> None:
    for role in _RUNTIME_ROLES:
        op.execute(
            sa.text(
                f"REVOKE ALL PRIVILEGES ON TABLE user_savepoint_commands FROM {role}"
            )
        )
    op.execute("DROP POLICY tenant_isolation ON user_savepoint_commands")
    op.drop_index(
        "ix_user_savepoint_commands_tenant_id",
        table_name="user_savepoint_commands",
    )
    op.drop_index(
        "ix_user_savepoint_commands_owner_run_status",
        table_name="user_savepoint_commands",
    )
    op.drop_table("user_savepoint_commands")
