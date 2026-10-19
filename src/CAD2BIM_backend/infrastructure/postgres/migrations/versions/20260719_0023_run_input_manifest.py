"""Persist the immutable server-compiled input closure on each new Run.

Revision ID: 20260719_0023
Revises: 20260719_0022
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260719_0023"
down_revision: str | None = "20260719_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "input_manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "runs",
        sa.Column("input_manifest_digest", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_runs_input_manifest_pair"),
        "runs",
        "(input_manifest IS NULL) = (input_manifest_digest IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_runs_input_manifest_digest_shape"),
        "runs",
        "input_manifest_digest IS NULL OR "
        "(length(input_manifest_digest) = 64 AND "
        "input_manifest_digest !~ '[^0-9a-f]')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_runs_input_manifest_digest_shape"),
        "runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_runs_input_manifest_pair"),
        "runs",
        type_="check",
    )
    op.drop_column("runs", "input_manifest_digest")
    op.drop_column("runs", "input_manifest")
