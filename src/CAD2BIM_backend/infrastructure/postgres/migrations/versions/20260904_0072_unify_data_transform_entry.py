"""Accept unified Data Transform repair Operation rows.

Revision ID: 20260904_0072
Revises: 20260904_0071
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260904_0072"
down_revision: str | None = "20260904_0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "critical_operations"
_DETAIL_CHECK = "ck_critical_operations_operation_kind_detail_valid"


def _detail_check(*, unified_transform: bool) -> str:
    base = (
        "(operation_kind = 'source_ingestion' AND tool_name = 'cad_import_source' "
        "AND source_filename IS NOT NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NULL AND code_artifact_version_id IS NULL "
        "AND code_artifact_sha256 IS NULL AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        "AND repair_parent_operation_id IS NULL "
        "AND base_ifc_revision_id IS NULL AND expected_ifc_write_epoch IS NULL "
        "AND target_geometry_resource_id IS NULL "
        "AND result_geometry_resource_version_id IS NULL)"
    )
    if unified_transform:
        transform_shape = (
            "((tool_name = 'cad_compute_measurements' "
            "AND repair_parent_operation_id IS NULL) OR "
            "(tool_name = 'data_repair_transform' "
            "AND description_artifact_id IS NOT NULL "
            "AND description_artifact_version_id IS NOT NULL "
            "AND description_artifact_sha256 IS NOT NULL "
            "AND repair_parent_operation_id IS NOT NULL) OR "
            "(tool_name = 'data_transform' AND "
            "(repair_parent_operation_id IS NULL OR "
            "(repair_parent_operation_id IS NOT NULL "
            "AND description_artifact_id IS NOT NULL "
            "AND description_artifact_version_id IS NOT NULL "
            "AND description_artifact_sha256 IS NOT NULL))))"
        )
    else:
        transform_shape = (
            "((tool_name = 'data_repair_transform' "
            "AND description_artifact_id IS NOT NULL "
            "AND description_artifact_version_id IS NOT NULL "
            "AND description_artifact_sha256 IS NOT NULL "
            "AND repair_parent_operation_id IS NOT NULL) OR "
            "(tool_name <> 'data_repair_transform' "
            "AND repair_parent_operation_id IS NULL))"
        )
    transform = (
        "(operation_kind = 'data_transform' "
        "AND tool_name IN ('data_transform', 'data_repair_transform', "
        "'cad_compute_measurements') AND source_filename IS NULL "
        "AND intent_summary IS NOT NULL AND code_artifact_id IS NOT NULL "
        "AND code_artifact_version_id IS NOT NULL AND code_artifact_sha256 IS NOT NULL "
        f"AND {transform_shape} "
        "AND base_ifc_revision_id IS NULL AND expected_ifc_write_epoch IS NULL "
        "AND target_geometry_resource_id IS NULL "
        "AND result_geometry_resource_version_id IS NULL)"
    )
    ifc = (
        "(operation_kind = 'ifc_model_write' AND source_filename IS NULL "
        "AND intent_summary IS NOT NULL AND code_artifact_id IS NULL "
        "AND code_artifact_version_id IS NULL AND code_artifact_sha256 IS NULL "
        "AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        "AND repair_parent_operation_id IS NULL AND result_package_id IS NULL "
        "AND target_geometry_resource_id IS NULL "
        "AND result_geometry_resource_version_id IS NULL "
        "AND ((tool_name = 'ifc_model_initialize' AND base_ifc_revision_id IS NULL "
        "AND expected_ifc_write_epoch IS NULL) OR "
        "(tool_name <> 'ifc_model_initialize' AND base_ifc_revision_id IS NOT NULL "
        "AND expected_ifc_write_epoch IS NOT NULL)))"
    )
    geometry = (
        "(operation_kind = 'geometry_resource_write' "
        "AND tool_name IN ('ifc_geometry_resource_create', "
        "'ifc_geometry_resource_repair', 'ifc_geometry_resource_write') "
        "AND source_filename IS NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NOT NULL AND code_artifact_version_id IS NOT NULL "
        "AND code_artifact_sha256 IS NOT NULL AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        "AND ((tool_name = 'ifc_geometry_resource_create' "
        "AND repair_parent_operation_id IS NULL) OR "
        "(tool_name = 'ifc_geometry_resource_repair' "
        "AND repair_parent_operation_id IS NOT NULL) OR "
        "tool_name = 'ifc_geometry_resource_write') "
        "AND result_package_id IS NULL AND base_ifc_revision_id IS NULL "
        "AND expected_ifc_write_epoch IS NULL "
        "AND target_geometry_resource_id IS NOT NULL)"
    )
    return f"{base} OR {transform} OR {ifc} OR {geometry}"


def _replace_detail_check(*, unified_transform: bool) -> None:
    op.drop_constraint(op.f(_DETAIL_CHECK), _TABLE, type_="check")
    op.create_check_constraint(
        op.f(_DETAIL_CHECK),
        _TABLE,
        _detail_check(unified_transform=unified_transform),
    )


def upgrade() -> None:
    _replace_detail_check(unified_transform=True)


def downgrade() -> None:
    op.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM {_TABLE}
                    WHERE tool_name = 'data_transform'
                      AND repair_parent_operation_id IS NOT NULL
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'unified Data Transform repair Operations exist';
                END IF;
            END
            $$
            """
        )
    )
    _replace_detail_check(unified_transform=False)
