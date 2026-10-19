"""Bind checkpoint links to immutable execution protocols.

Revision ID: 20260719_0017
Revises: 20260718_0016
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260719_0017"
down_revision: str | None = "20260718_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "checkpoint_links",
        sa.Column("execution_protocol_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "checkpoint_links",
        sa.Column("execution_protocol_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "checkpoint_links",
        sa.Column("topology_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_checkpoint_links_execution_protocol_binding_complete"),
        "checkpoint_links",
        "(execution_protocol_id IS NULL "
        "AND execution_protocol_digest IS NULL "
        "AND topology_fingerprint IS NULL) OR "
        "(execution_protocol_id IS NOT NULL "
        "AND execution_protocol_digest IS NOT NULL "
        "AND topology_fingerprint IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_checkpoint_links_execution_protocol_digest_shape"),
        "checkpoint_links",
        "execution_protocol_digest IS NULL OR execution_protocol_digest ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        op.f("ck_checkpoint_links_topology_fingerprint_shape"),
        "checkpoint_links",
        "topology_fingerprint IS NULL OR topology_fingerprint ~ '^[0-9a-f]{64}$'",
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION prevent_checkpoint_link_protocol_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF OLD.execution_protocol_id IS NOT NULL
                   AND (
                       NEW.execution_protocol_id IS DISTINCT FROM
                           OLD.execution_protocol_id
                       OR NEW.execution_protocol_digest IS DISTINCT FROM
                           OLD.execution_protocol_digest
                       OR NEW.topology_fingerprint IS DISTINCT FROM
                           OLD.topology_fingerprint
                   )
                THEN
                    RAISE EXCEPTION
                        'checkpoint execution protocol fields are immutable'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_checkpoint_links_execution_protocol_immutable
            BEFORE UPDATE OF
                execution_protocol_id,
                execution_protocol_digest,
                topology_fingerprint
            ON checkpoint_links
            FOR EACH ROW
            EXECUTE FUNCTION prevent_checkpoint_link_protocol_mutation()
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER trg_checkpoint_links_execution_protocol_immutable ON checkpoint_links"
        )
    )
    op.execute(sa.text("DROP FUNCTION prevent_checkpoint_link_protocol_mutation()"))
    op.drop_constraint(
        op.f("ck_checkpoint_links_topology_fingerprint_shape"),
        "checkpoint_links",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_checkpoint_links_execution_protocol_digest_shape"),
        "checkpoint_links",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_checkpoint_links_execution_protocol_binding_complete"),
        "checkpoint_links",
        type_="check",
    )
    op.drop_column("checkpoint_links", "topology_fingerprint")
    op.drop_column("checkpoint_links", "execution_protocol_digest")
    op.drop_column("checkpoint_links", "execution_protocol_id")
