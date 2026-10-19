"""Persist exact Composite Component source lineage.

Revision ID: 20260814_0057
Revises: 20260814_0056
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260814_0057"
down_revision: str | None = "20260814_0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing Composite rows cannot be assigned trustworthy child-version
    # authority after the fact. This pre-release state must be rebuilt instead
    # of receiving a fabricated runtime compatibility lineage.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM component_catalog_events
                    WHERE authoring_kind = 'composite'
                ) THEN
                    RAISE EXCEPTION
                        'Composite Component source lineage requires rebuilding '
                        'pre-cut catalog rows before migration';
                END IF;
            END
            $$
            """
        )
    )
    op.add_column(
        "component_catalog_events",
        sa.Column(
            "source_authorities_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_component_catalog_events_source_authorities_valid"),
        "component_catalog_events",
        "jsonb_typeof(source_authorities_json) = 'array' AND "
        "((authoring_kind = 'composite' "
        "AND jsonb_array_length(source_authorities_json) > 0) OR "
        "(authoring_kind <> 'composite' "
        "AND jsonb_array_length(source_authorities_json) = 0))",
    )
    op.alter_column(
        "component_catalog_events",
        "source_authorities_json",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_component_catalog_events_source_authorities_valid"),
        "component_catalog_events",
        type_="check",
    )
    op.drop_column("component_catalog_events", "source_authorities_json")
