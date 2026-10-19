"""Remove the retired safe-point publication discriminator.

Revision ID: 20260811_0051
Revises: 20260811_0050
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260811_0051"
down_revision: str | None = "20260811_0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The removed value was a constant row discriminator. It was not part of
    # the prepare request fingerprint, checkpoint cursor, Snapshot identity or
    # restore link, so existing semantic rows remain exact and can be retained.
    op.drop_constraint(
        op.f("ck_safe_point_publications_schema_version_valid"),
        "safe_point_publications",
        type_="check",
    )
    op.drop_column("safe_point_publications", "schema_version")


def downgrade() -> None:
    op.add_column(
        "safe_point_publications",
        sa.Column(
            "schema_version",
            sa.String(length=16),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_check_constraint(
        op.f("ck_safe_point_publications_schema_version_valid"),
        "safe_point_publications",
        "schema_version = '1'",
    )
