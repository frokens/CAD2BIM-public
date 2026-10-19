"""Enforce one personal tenant for each principal.

Revision ID: 20260719_0019
Revises: 20260719_0018
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "20260719_0019"
down_revision: str | None = "20260719_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_tenant_principals_principal",
        "tenant_principals",
        ["principal_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_tenant_principals_principal",
        "tenant_principals",
        type_="unique",
    )
