"""Persist the LangGraph checkpoint namespace on savepoint links.

Revision ID: 20260711_0002
Revises: 20260711_0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260711_0002"
down_revision: str | None = "20260711_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "checkpoint_links",
        sa.Column(
            "checkpoint_ns",
            sa.String(length=255),
            server_default=sa.text("''"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("checkpoint_links", "checkpoint_ns")
