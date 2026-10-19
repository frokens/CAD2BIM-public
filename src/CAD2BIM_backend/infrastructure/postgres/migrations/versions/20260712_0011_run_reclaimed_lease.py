"""Persist whether the active claim reclaimed an expired running lease.

Revision ID: 20260712_0011
Revises: 20260712_0010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260712_0011"
down_revision: str | None = "20260712_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "reclaimed_lease",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "reclaimed_lease")
