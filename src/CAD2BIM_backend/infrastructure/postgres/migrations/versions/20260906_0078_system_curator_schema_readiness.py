"""Allow the constrained SYSTEM curator to verify the Alembic schema revision.

Revision ID: 20260906_0078
Revises: 20260905_0077
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260906_0078"
down_revision: str | None = "20260905_0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("GRANT SELECT ON TABLE alembic_version TO cad2bim_system_curator"))


def downgrade() -> None:
    op.execute(sa.text("REVOKE SELECT ON TABLE alembic_version FROM cad2bim_system_curator"))
