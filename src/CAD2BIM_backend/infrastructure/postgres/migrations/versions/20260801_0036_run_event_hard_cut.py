"""Hard-cut the append-only RunEvent table to the single current contract.

Revision ID: 20260801_0036
Revises: 20260801_0035
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260801_0036"
down_revision: str | None = "20260801_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CAD2BIM has not been released.  Rows from the superseded event contract
    # cannot be made authoritative without inventing item identity or audience,
    # so development event history is quarantined by rebuilding this projection.
    op.execute(sa.text("DELETE FROM run_events"))

    op.drop_constraint(
        op.f("ck_run_events_v2_fields_present"),
        "run_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_run_events_audience_valid"),
        "run_events",
        type_="check",
    )
    op.drop_column("run_events", "node")
    op.drop_column("run_events", "schema_version")

    op.alter_column("run_events", "event_type", nullable=False)
    op.alter_column("run_events", "audience", nullable=False)
    op.alter_column("run_events", "item_id", nullable=False)
    op.alter_column(
        "run_events",
        "summary",
        existing_type=sa.Text(),
        nullable=False,
        server_default="",
    )
    op.alter_column("run_events", "summary", server_default=None)
    op.create_check_constraint(
        op.f("ck_run_events_audience_valid"),
        "run_events",
        "audience IN ('public', 'operator', 'internal')",
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM run_events"))

    op.drop_constraint(
        op.f("ck_run_events_audience_valid"),
        "run_events",
        type_="check",
    )
    op.add_column(
        "run_events",
        sa.Column(
            "schema_version",
            sa.String(length=16),
            nullable=False,
            server_default="2",
        ),
    )
    op.add_column(
        "run_events",
        sa.Column(
            "node",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("run_events", "schema_version", server_default=None)
    op.alter_column("run_events", "node", server_default=None)
    op.alter_column("run_events", "summary", nullable=True)
    op.alter_column("run_events", "item_id", nullable=True)
    op.alter_column("run_events", "audience", nullable=True)
    op.alter_column("run_events", "event_type", nullable=True)
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
