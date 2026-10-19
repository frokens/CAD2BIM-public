"""Bind every new Savepoint to one exact immutable resume manifest.

Revision ID: 20260730_0033
Revises: 20260728_0032
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260730_0033"
down_revision: str | None = "20260728_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "checkpoint_links",
        sa.Column("manifest_artifact_id", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "checkpoint_links",
        sa.Column("manifest_version_no", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "checkpoint_links",
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_checkpoint_links_owner_task_manifest_artifact",
        "checkpoint_links",
        "artifacts",
        ["owner_id", "task_id", "manifest_artifact_id"],
        ["owner_id", "task_id", "artifact_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_checkpoint_links_manifest_binding_complete"),
        "checkpoint_links",
        "(manifest_artifact_id IS NULL "
        "AND manifest_version_no IS NULL "
        "AND manifest_sha256 IS NULL) OR "
        "(manifest_artifact_id IS NOT NULL "
        "AND manifest_version_no IS NOT NULL "
        "AND manifest_sha256 IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_checkpoint_links_manifest_version_positive"),
        "checkpoint_links",
        "manifest_version_no IS NULL OR manifest_version_no > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_checkpoint_links_manifest_version_positive"),
        "checkpoint_links",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_checkpoint_links_manifest_binding_complete"),
        "checkpoint_links",
        type_="check",
    )
    op.drop_constraint(
        "fk_checkpoint_links_owner_task_manifest_artifact",
        "checkpoint_links",
        type_="foreignkey",
    )
    op.drop_column("checkpoint_links", "manifest_sha256")
    op.drop_column("checkpoint_links", "manifest_version_no")
    op.drop_column("checkpoint_links", "manifest_artifact_id")
