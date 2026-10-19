"""Hard-cut feedback commands to explicit steer/interrupt delivery batches.

Revision ID: 20260727_0029
Revises: 20260727_0028
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260727_0029"
down_revision: str | None = "20260727_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNTIME_ROLES = ("cad2bim_api_runtime", "cad2bim_runner_runtime")
_TENANT_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '')"
)


def _enable_tenant_rls(table_name: str) -> None:
    op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"""
            CREATE POLICY tenant_isolation ON {table_name}
            USING ({_TENANT_EXPRESSION})
            WITH CHECK ({_TENANT_EXPRESSION})
            """
        )
    )
    for role in _RUNTIME_ROLES:
        op.execute(
            sa.text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table_name} TO {role}"
            )
        )


def upgrade() -> None:
    # Old rows cannot reveal whether the user clicked steer or interrupt.
    # Preserve them as an unreachable development quarantine instead of
    # inventing a delivery mode or maintaining a runtime compatibility reader.
    op.rename_table("feedback", "feedback_migration_quarantine")
    for role in _RUNTIME_ROLES:
        op.execute(
            sa.text(
                f"REVOKE ALL PRIVILEGES ON TABLE feedback_migration_quarantine FROM {role}"
            )
        )

    op.create_table(
        "feedback_commands",
        sa.Column("feedback_id", sa.String(length=80), nullable=False),
        sa.Column("accepted_sequence", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("author_principal_id", sa.String(length=80), nullable=False),
        sa.Column("client_request_id", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("delivery_mode", sa.String(length=32), nullable=False),
        sa.Column("resolved_scope", sa.JSON(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("claimed_batch_id", sa.String(length=80), nullable=True),
        sa.Column("applied_checkpoint_id", sa.String(length=255), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "accepted_sequence > 0",
            name=op.f("ck_feedback_commands_accepted_sequence_positive"),
        ),
        sa.CheckConstraint(
            "delivery_mode IN ('steer', 'interrupt')",
            name=op.f("ck_feedback_commands_delivery_mode_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'claimed', 'applied', 'superseded')",
            name=op.f("ck_feedback_commands_status_valid"),
        ),
        sa.CheckConstraint(
            "((status = 'accepted' AND claimed_batch_id IS NULL "
            "AND applied_checkpoint_id IS NULL AND applied_at IS NULL) OR "
            "(status = 'claimed' AND claimed_batch_id IS NOT NULL "
            "AND applied_checkpoint_id IS NULL AND applied_at IS NULL) OR "
            "(status = 'applied' AND claimed_batch_id IS NOT NULL "
            "AND applied_checkpoint_id IS NOT NULL AND applied_at IS NOT NULL) OR "
            "(status = 'superseded' AND claimed_batch_id IS NULL "
            "AND applied_checkpoint_id IS NULL AND applied_at IS NULL))",
            name=op.f("ck_feedback_commands_lifecycle_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_feedback_commands_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_feedback_commands_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_feedback_commands_owner_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_feedback_commands_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "author_principal_id"],
            ["tenant_principals.tenant_id", "tenant_principals.principal_id"],
            name="fk_feedback_commands_tenant_author",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "feedback_id",
            name=op.f("pk_feedback_commands"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "author_principal_id",
            "client_request_id",
            name="uq_feedback_commands_idempotency",
        ),
        sa.UniqueConstraint(
            "run_id",
            "accepted_sequence",
            name="uq_feedback_commands_run_sequence",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "feedback_id",
            name="uq_feedback_commands_tenant_run_feedback",
        ),
    )
    op.create_index(
        "ix_feedback_commands_owner_run_status",
        "feedback_commands",
        ["owner_id", "run_id", "status"],
    )
    op.create_index(
        "ix_feedback_commands_owner_run_sequence",
        "feedback_commands",
        ["owner_id", "run_id", "accepted_sequence"],
    )
    op.create_index(
        op.f("ix_feedback_commands_tenant_id"),
        "feedback_commands",
        ["tenant_id"],
    )

    op.create_table(
        "feedback_delivery_batches",
        sa.Column("batch_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("target_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("claim_high_watermark", sa.BigInteger(), nullable=False),
        sa.Column("contains_interrupt", sa.Boolean(), nullable=False),
        sa.Column("claimed_writer_generation", sa.BigInteger(), nullable=False),
        sa.Column("claimed_checkpoint_id", sa.String(length=255), nullable=True),
        sa.Column("applied_checkpoint_id", sa.String(length=255), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "claim_high_watermark > 0",
            name=op.f("ck_feedback_delivery_batches_watermark_positive"),
        ),
        sa.CheckConstraint(
            "claimed_writer_generation > 0",
            name=op.f("ck_feedback_delivery_batches_writer_generation_positive"),
        ),
        sa.CheckConstraint(
            "(applied_checkpoint_id IS NULL) = (applied_at IS NULL)",
            name=op.f("ck_feedback_delivery_batches_applied_fields_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_feedback_delivery_batches_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_feedback_delivery_batches_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_feedback_delivery_batches_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "batch_id",
            name=op.f("pk_feedback_delivery_batches"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "batch_id",
            name="uq_feedback_delivery_batches_tenant_run_batch",
        ),
        sa.UniqueConstraint(
            "run_id",
            "target_fingerprint",
            "claim_high_watermark",
            "claimed_writer_generation",
            name="uq_feedback_delivery_batches_replay",
        ),
    )
    op.create_index(
        "ix_feedback_delivery_batches_owner_run_created",
        "feedback_delivery_batches",
        ["owner_id", "run_id", "created_at"],
    )
    op.create_index(
        op.f("ix_feedback_delivery_batches_tenant_id"),
        "feedback_delivery_batches",
        ["tenant_id"],
    )

    op.create_table(
        "feedback_delivery_batch_members",
        sa.Column("batch_id", sa.String(length=80), nullable=False),
        sa.Column("feedback_id", sa.String(length=80), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("accepted_sequence", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "accepted_sequence > 0",
            name=op.f(
                "ck_feedback_delivery_batch_members_accepted_sequence_positive"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f(
                "fk_feedback_delivery_batch_members_tenant_id_tenants"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "batch_id"],
            [
                "feedback_delivery_batches.tenant_id",
                "feedback_delivery_batches.run_id",
                "feedback_delivery_batches.batch_id",
            ],
            name="fk_feedback_delivery_batch_members_batch",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "feedback_id"],
            [
                "feedback_commands.tenant_id",
                "feedback_commands.run_id",
                "feedback_commands.feedback_id",
            ],
            name="fk_feedback_delivery_batch_members_command",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "batch_id",
            "feedback_id",
            name=op.f("pk_feedback_delivery_batch_members"),
        ),
        sa.UniqueConstraint(
            "batch_id",
            "accepted_sequence",
            name="uq_feedback_delivery_batch_members_sequence",
        ),
    )
    op.create_index(
        op.f("ix_feedback_delivery_batch_members_tenant_id"),
        "feedback_delivery_batch_members",
        ["tenant_id"],
    )

    for table_name in (
        "feedback_commands",
        "feedback_delivery_batches",
        "feedback_delivery_batch_members",
    ):
        _enable_tenant_rls(table_name)


def downgrade() -> None:
    for table_name in (
        "feedback_delivery_batch_members",
        "feedback_delivery_batches",
        "feedback_commands",
    ):
        op.drop_table(table_name)
    op.rename_table("feedback_migration_quarantine", "feedback")
    for role in _RUNTIME_ROLES:
        op.execute(
            sa.text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE feedback TO {role}"
            )
        )
