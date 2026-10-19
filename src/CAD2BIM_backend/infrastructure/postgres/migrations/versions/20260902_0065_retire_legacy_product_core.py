"""Retire the empty legacy Product-Core schema without guessing lineage.

Revision ID: 20260902_0065
Revises: 20260830_0064
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260902_0065"
down_revision: str | None = "20260830_0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            LOCK TABLE
                run_component_views,
                component_catalog_events,
                component_publication_receipts,
                component_promotions
            IN ACCESS EXCLUSIVE MODE
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                run_component_views_count bigint;
                component_catalog_events_count bigint;
                component_publication_receipts_count bigint;
                component_promotions_count bigint;
            BEGIN
                SELECT count(*) INTO run_component_views_count
                FROM run_component_views;
                SELECT count(*) INTO component_catalog_events_count
                FROM component_catalog_events;
                SELECT count(*) INTO component_publication_receipts_count
                FROM component_publication_receipts;
                SELECT count(*) INTO component_promotions_count
                FROM component_promotions;

                IF run_component_views_count <> 0
                   OR component_catalog_events_count <> 0
                   OR component_publication_receipts_count <> 0
                   OR component_promotions_count <> 0 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = format(
                            'legacy Product-Core tables are not empty: '
                            'run_component_views=%s, component_catalog_events=%s, '
                            'component_publication_receipts=%s, component_promotions=%s',
                            run_component_views_count,
                            component_catalog_events_count,
                            component_publication_receipts_count,
                            component_promotions_count
                        );
                END IF;
            END
            $$
            """
        )
    )
    op.drop_table("component_publication_receipts")
    op.drop_table("component_promotions")
    op.drop_table("component_catalog_events")
    op.drop_table("run_component_views")


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                RAISE EXCEPTION USING
                    ERRCODE = '0A000',
                    MESSAGE = '20260902_0065 is intentionally irreversible; '
                              'restore the exact 0064 database from a backup or preserved worktree';
            END
            $$
            """
        )
    )
