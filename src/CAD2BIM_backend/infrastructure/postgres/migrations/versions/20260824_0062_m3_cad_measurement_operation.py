"""Allow the M3 measurement tool to publish data_transform Operations.

Revision ID: 20260824_0062
Revises: 20260823_0061
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260824_0062"
down_revision: str | None = "20260823_0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT = "ck_critical_operations_operation_kind_detail_valid"


def _replace_tool_constraint(*, include_measurement: bool) -> None:
    op.drop_constraint(op.f(_CONSTRAINT), "critical_operations", type_="check")
    transform_tool_predicate = (
        "tool_name IN ('data_transform', 'cad_compute_measurements')"
        if include_measurement
        else "tool_name = 'data_transform'"
    )
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        "critical_operations",
        "(operation_kind = 'source_ingestion' AND tool_name = 'cad_import_source' "
        "AND source_filename IS NOT NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NULL AND code_artifact_version_id IS NULL "
        "AND code_artifact_sha256 IS NULL) OR "
        f"(operation_kind = 'data_transform' AND {transform_tool_predicate} "
        "AND source_filename IS NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NOT NULL AND code_artifact_version_id IS NOT NULL "
        "AND code_artifact_sha256 IS NOT NULL)",
    )


def upgrade() -> None:
    _replace_tool_constraint(include_measurement=True)


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM critical_operations
                    WHERE tool_name = 'cad_compute_measurements'
                ) THEN
                    RAISE EXCEPTION
                        'cannot downgrade 0062 while M3 measurement Operations exist';
                END IF;
            END
            $$
            """
        )
    )
    _replace_tool_constraint(include_measurement=False)
