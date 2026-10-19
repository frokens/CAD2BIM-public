"""Persist reusable Component Family and immutable Variant identity.

Revision ID: 20260815_0058
Revises: 20260814_0057
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260815_0058"
down_revision: str | None = "20260814_0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_empty_component_catalog() -> None:
    # A pre-cut row has no trustworthy canonical parameter bytes, generator
    # authority, builder policy, or Variant digest.  This pre-release state is
    # rebuilt instead of receiving a fabricated compatibility identity.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM component_catalog_events LIMIT 1) THEN
                    RAISE EXCEPTION
                        'Component Family/Variant identity requires rebuilding '
                        'pre-cut catalog rows before migration';
                END IF;
            END
            $$
            """
        )
    )


def upgrade() -> None:
    _require_empty_component_catalog()
    for column_name in (
        "family_contract_digest",
        "parameter_digest",
        "variant_digest",
    ):
        op.add_column(
            "run_component_views",
            sa.Column(column_name, sa.String(length=71), nullable=False),
        )
    op.create_check_constraint(
        op.f("ck_run_component_views_component_variant_identity_valid"),
        "run_component_views",
        "family_contract_digest ~ '^sha256:[0-9a-f]{64}$' "
        "AND parameter_digest ~ '^sha256:[0-9a-f]{64}$' "
        "AND variant_digest ~ '^sha256:[0-9a-f]{64}$' "
        "AND version = variant_digest",
    )
    op.add_column(
        "component_catalog_events",
        sa.Column("definition_id", sa.String(length=80), nullable=False),
    )
    op.add_column(
        "component_catalog_events",
        sa.Column(
            "family_contract_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
    )
    op.add_column(
        "component_catalog_events",
        sa.Column("builder_policy_digest", sa.String(length=71), nullable=False),
    )
    op.add_column(
        "component_catalog_events",
        sa.Column(
            "canonical_parameters_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
    )
    op.add_column(
        "component_catalog_events",
        sa.Column("parameter_digest", sa.String(length=71), nullable=False),
    )
    op.add_column(
        "component_catalog_events",
        sa.Column("variant_digest", sa.String(length=71), nullable=False),
    )
    op.add_column(
        "component_catalog_events",
        sa.Column(
            "exact_child_variants_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_component_catalog_events_component_definition_identity_valid"),
        "component_catalog_events",
        "definition_id ~ '^component_definition_[0-9a-f]{32}$'",
    )
    op.create_check_constraint(
        op.f("ck_component_catalog_events_component_variant_identity_valid"),
        "component_catalog_events",
        "builder_policy_digest ~ '^sha256:[0-9a-f]{64}$' "
        "AND parameter_digest ~ '^sha256:[0-9a-f]{64}$' "
        "AND variant_digest ~ '^sha256:[0-9a-f]{64}$' "
        "AND version = variant_digest",
    )
    op.drop_column("component_catalog_events", "parameter_schema_json")


def downgrade() -> None:
    _require_empty_component_catalog()
    op.add_column(
        "component_catalog_events",
        sa.Column("parameter_schema_json", sa.JSON(), nullable=False),
    )
    op.drop_constraint(
        op.f("ck_component_catalog_events_component_definition_identity_valid"),
        "component_catalog_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_component_catalog_events_component_variant_identity_valid"),
        "component_catalog_events",
        type_="check",
    )
    for column_name in (
        "exact_child_variants_json",
        "variant_digest",
        "parameter_digest",
        "canonical_parameters_json",
        "builder_policy_digest",
        "family_contract_json",
        "definition_id",
    ):
        op.drop_column("component_catalog_events", column_name)
    op.drop_constraint(
        op.f("ck_run_component_views_component_variant_identity_valid"),
        "run_component_views",
        type_="check",
    )
    for column_name in (
        "variant_digest",
        "parameter_digest",
        "family_contract_digest",
    ):
        op.drop_column("run_component_views", column_name)
