"""Fence Component publication and persist exact replay receipts.

Revision ID: 20260814_0056
Revises: 20260812_0055
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260814_0056"
down_revision: str | None = "20260812_0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_EXPRESSION = (
    "tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '')"
)


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
                IF EXISTS (SELECT 1 FROM component_promotions LIMIT 1) THEN
                    populated := array_append(populated, 'component_promotions');
                END IF;
                IF cardinality(populated) > 0 THEN
                    RAISE EXCEPTION
                        'Component publication fencing requires empty pre-release '
                        'Component projections; back up and rebuild before retrying '
                        '(populated: %)', array_to_string(populated, ', ');
                END IF;
            END
            $$
            """
        )
    )


def upgrade() -> None:
    # Existing development rows have no trustworthy invocation/session frontier.
    # They are deliberately not backfilled through a runtime compatibility path.
    _require_empty_pre_cut_component_state()

    for column in (
        sa.Column("source_invocation_id", sa.String(length=80), nullable=True),
        sa.Column("source_invocation_revision", sa.BigInteger(), nullable=True),
        sa.Column("source_invocation_digest", sa.String(length=64), nullable=True),
        sa.Column("source_catalog_proof_digest", sa.String(length=64), nullable=True),
        sa.Column("source_session_id", sa.String(length=80), nullable=True),
        sa.Column("source_writer_id", sa.String(length=255), nullable=True),
        sa.Column("source_writer_generation", sa.BigInteger(), nullable=True),
    ):
        op.add_column("component_catalog_events", column)
    op.create_foreign_key(
        "fk_component_catalog_events_owner_task_session",
        "component_catalog_events",
        "mcp_sessions",
        ["owner_id", "task_id", "source_session_id"],
        ["owner_id", "task_id", "session_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_component_catalog_events_publication_authority_complete"),
        "component_catalog_events",
        "((source_run_id IS NULL AND source_stage_key IS NULL "
        "AND source_invocation_id IS NULL AND source_invocation_revision IS NULL "
        "AND source_invocation_digest IS NULL AND source_catalog_proof_digest IS NULL "
        "AND source_session_id IS NULL AND source_writer_id IS NULL "
        "AND source_writer_generation IS NULL) OR "
        "(source_run_id IS NOT NULL AND source_stage_key IS NOT NULL "
        "AND source_invocation_id IS NOT NULL "
        "AND source_invocation_revision IS NOT NULL "
        "AND source_invocation_digest IS NOT NULL "
        "AND source_session_id IS NOT NULL AND source_writer_id IS NOT NULL "
        "AND source_writer_generation IS NOT NULL))",
    )
    op.create_check_constraint(
        op.f("ck_component_catalog_events_source_invocation_revision_positive"),
        "component_catalog_events",
        "source_invocation_revision IS NULL OR source_invocation_revision > 0",
    )
    op.create_check_constraint(
        op.f("ck_component_catalog_events_source_writer_generation_positive"),
        "component_catalog_events",
        "source_writer_generation IS NULL OR source_writer_generation > 0",
    )
    op.create_check_constraint(
        op.f("ck_component_catalog_events_publication_proof_scope_valid"),
        "component_catalog_events",
        "source_run_id IS NULL OR "
        "(scope = 'task' AND source_catalog_proof_digest IS NOT NULL) OR "
        "(scope = 'user' AND source_catalog_proof_digest IS NULL)",
    )

    op.create_table(
        "component_publication_receipts",
        sa.Column("publication_receipt_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("publication_scope", sa.String(length=32), nullable=False),
        sa.Column("publication_principal_id", sa.String(length=80), nullable=True),
        sa.Column("actor_principal_id", sa.String(length=80), nullable=False),
        sa.Column("catalog_scope_key", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("invocation_id", sa.String(length=80), nullable=False),
        sa.Column("invocation_revision", sa.BigInteger(), nullable=False),
        sa.Column("invocation_digest", sa.String(length=64), nullable=False),
        sa.Column("catalog_proof_digest", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=80), nullable=False),
        sa.Column("first_writer_id", sa.String(length=255), nullable=False),
        sa.Column("first_writer_generation", sa.BigInteger(), nullable=False),
        sa.Column("catalog_entry_id", sa.String(length=80), nullable=False),
        sa.Column("result_catalog_revision", sa.BigInteger(), nullable=False),
        sa.Column("event_appended", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.CheckConstraint(
            "((publication_scope = 'task' AND publication_principal_id IS NULL "
            "AND catalog_scope_key = task_id AND catalog_proof_digest IS NOT NULL) "
            "OR (publication_scope = 'user' AND publication_principal_id IS NOT NULL "
            "AND catalog_scope_key = publication_principal_id "
            "AND catalog_proof_digest IS NULL))",
            name=op.f("ck_component_publication_receipts_scope_valid"),
        ),
        sa.CheckConstraint(
            "invocation_revision > 0",
            name=op.f("ck_component_publication_receipts_invocation_revision_positive"),
        ),
        sa.CheckConstraint(
            "first_writer_generation > 0",
            name=op.f("ck_component_publication_receipts_writer_generation_positive"),
        ),
        sa.CheckConstraint(
            "result_catalog_revision > 0",
            name=op.f("ck_component_publication_receipts_catalog_revision_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_component_publication_receipts_owner_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_component_publication_receipts_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_component_publication_receipts_owner_task_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "session_id"],
            ["mcp_sessions.owner_id", "mcp_sessions.task_id", "mcp_sessions.session_id"],
            name="fk_component_publication_receipts_owner_task_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_entry_id"],
            ["component_catalog_events.catalog_entry_id"],
            name="fk_component_publication_receipts_catalog_entry",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "actor_principal_id"],
            ["tenant_principals.tenant_id", "tenant_principals.principal_id"],
            name="fk_component_publication_receipts_actor_principal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "publication_principal_id"],
            ["tenant_principals.tenant_id", "tenant_principals.principal_id"],
            name="fk_component_publication_receipts_publication_principal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_component_publication_receipts_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "publication_receipt_id",
            name=op.f("pk_component_publication_receipts"),
        ),
        sa.UniqueConstraint(
            "owner_id",
            "publication_scope",
            "catalog_scope_key",
            "idempotency_key",
            name="uq_component_publication_receipts_scope_idempotency",
        ),
    )
    op.create_index(
        op.f("ix_component_publication_receipts_tenant_id"),
        "component_publication_receipts",
        ["tenant_id"],
    )
    op.create_index(
        "ix_component_publication_receipts_owner_run_created",
        "component_publication_receipts",
        ["owner_id", "run_id", "created_at"],
    )
    op.execute(
        "ALTER TABLE component_publication_receipts ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE component_publication_receipts FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        sa.text(
            f"""
            CREATE POLICY tenant_isolation ON component_publication_receipts
            USING ({_TENANT_EXPRESSION})
            WITH CHECK ({_TENANT_EXPRESSION})
            """
        )
    )
    op.execute(
        "GRANT SELECT, INSERT ON TABLE component_publication_receipts "
        "TO cad2bim_api_runtime"
    )
    op.execute(
        "GRANT SELECT ON TABLE component_publication_receipts "
        "TO cad2bim_runner_runtime"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT ON TABLE component_publication_receipts "
        "FROM cad2bim_runner_runtime"
    )
    op.execute(
        "REVOKE SELECT, INSERT ON TABLE component_publication_receipts "
        "FROM cad2bim_api_runtime"
    )
    op.execute("DROP POLICY tenant_isolation ON component_publication_receipts")
    op.drop_index(
        "ix_component_publication_receipts_owner_run_created",
        table_name="component_publication_receipts",
    )
    op.drop_index(
        op.f("ix_component_publication_receipts_tenant_id"),
        table_name="component_publication_receipts",
    )
    op.drop_table("component_publication_receipts")

    for constraint_name, constraint_type in (
        (
            op.f("ck_component_catalog_events_publication_proof_scope_valid"),
            "check",
        ),
        (
            op.f("ck_component_catalog_events_source_writer_generation_positive"),
            "check",
        ),
        (
            op.f("ck_component_catalog_events_source_invocation_revision_positive"),
            "check",
        ),
        (
            op.f("ck_component_catalog_events_publication_authority_complete"),
            "check",
        ),
        ("fk_component_catalog_events_owner_task_session", "foreignkey"),
    ):
        op.drop_constraint(
            constraint_name,
            "component_catalog_events",
            type_=constraint_type,
        )
    for column_name in (
        "source_writer_generation",
        "source_writer_id",
        "source_session_id",
        "source_catalog_proof_digest",
        "source_invocation_digest",
        "source_invocation_revision",
        "source_invocation_id",
    ):
        op.drop_column("component_catalog_events", column_name)
