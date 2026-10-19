"""Add versioned RunEventV2 work-item fields.

Revision ID: 20260712_0012
Revises: 20260712_0011
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260712_0012"
down_revision: str | None = "20260712_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run_events", sa.Column("event_type", sa.String(length=200)))
    op.add_column("run_events", sa.Column("audience", sa.String(length=16)))
    op.add_column("run_events", sa.Column("item_id", sa.String(length=255)))
    op.add_column("run_events", sa.Column("parent_item_id", sa.String(length=255)))
    op.add_column("run_events", sa.Column("summary", sa.Text()))
    op.create_check_constraint(
        op.f("ck_run_events_audience_valid"),
        "run_events",
        "audience IS NULL OR audience IN ('public', 'operator', 'internal')",
    )
    op.create_check_constraint(
        op.f("ck_run_events_v2_fields_present"),
        "run_events",
        "schema_version <> '2' OR "
        "(event_type IS NOT NULL AND audience IS NOT NULL AND item_id IS NOT NULL)",
    )
    op.create_index(
        "ix_run_events_run_item",
        "run_events",
        ["run_id", "item_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_run_events_run_item", table_name="run_events")
    op.drop_constraint(op.f("ck_run_events_v2_fields_present"), "run_events", type_="check")
    op.drop_constraint(op.f("ck_run_events_audience_valid"), "run_events", type_="check")
    op.drop_column("run_events", "summary")
    op.drop_column("run_events", "parent_item_id")
    op.drop_column("run_events", "item_id")
    op.drop_column("run_events", "audience")
    op.drop_column("run_events", "event_type")
