"""Accept the unified Geometry Resource write Operation tool name.

Revision ID: 20260903_0070
Revises: 20260903_0069
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260903_0070"
down_revision: str | None = "20260903_0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "critical_operations"
_DETAIL_CHECK = "ck_critical_operations_operation_kind_detail_valid"
_UNIFIED_TOOL = "ifc_geometry_resource_write"


def _detail_check(*, unified: bool) -> str:
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
    transform = (
        "(operation_kind = 'data_transform' "
        "AND tool_name IN ('data_transform', 'data_repair_transform', "
        "'cad_compute_measurements') AND source_filename IS NULL "
        "AND intent_summary IS NOT NULL AND code_artifact_id IS NOT NULL "
        "AND code_artifact_version_id IS NOT NULL AND code_artifact_sha256 IS NOT NULL "
        "AND ((tool_name = 'data_repair_transform' "
        "AND description_artifact_id IS NOT NULL "
        "AND description_artifact_version_id IS NOT NULL "
        "AND description_artifact_sha256 IS NOT NULL "
        "AND repair_parent_operation_id IS NOT NULL) OR "
        "(tool_name <> 'data_repair_transform' AND repair_parent_operation_id IS NULL)) "
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
    tool_names = (
        "'ifc_geometry_resource_create', 'ifc_geometry_resource_repair', "
        "'ifc_geometry_resource_write'"
        if unified
        else "'ifc_geometry_resource_create', 'ifc_geometry_resource_repair'"
    )
    repair_shape = (
        "((tool_name = 'ifc_geometry_resource_create' "
        "AND repair_parent_operation_id IS NULL) OR "
        "(tool_name = 'ifc_geometry_resource_repair' "
        "AND repair_parent_operation_id IS NOT NULL) OR "
        "tool_name = 'ifc_geometry_resource_write')"
        if unified
        else "((tool_name = 'ifc_geometry_resource_create' "
        "AND repair_parent_operation_id IS NULL) OR "
        "(tool_name = 'ifc_geometry_resource_repair' "
        "AND repair_parent_operation_id IS NOT NULL))"
    )
    geometry = (
        "(operation_kind = 'geometry_resource_write' "
        f"AND tool_name IN ({tool_names}) AND source_filename IS NULL "
        "AND intent_summary IS NOT NULL AND code_artifact_id IS NOT NULL "
        "AND code_artifact_version_id IS NOT NULL AND code_artifact_sha256 IS NOT NULL "
        "AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        f"AND {repair_shape} "
        "AND result_package_id IS NULL AND base_ifc_revision_id IS NULL "
        "AND expected_ifc_write_epoch IS NULL "
        "AND target_geometry_resource_id IS NOT NULL)"
    )
    return f"{base} OR {transform} OR {ifc} OR {geometry}"


def _replace_detail_check(*, unified: bool) -> None:
    op.drop_constraint(op.f(_DETAIL_CHECK), _TABLE, type_="check")
    op.create_check_constraint(
        op.f(_DETAIL_CHECK),
        _TABLE,
        _detail_check(unified=unified),
    )


def upgrade() -> None:
    _replace_detail_check(unified=True)


def downgrade() -> None:
    op.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM {_TABLE} WHERE tool_name = '{_UNIFIED_TOOL}'
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'unified geometry resource write Operations exist';
                END IF;
            END
            $$
            """
        )
    )
    _replace_detail_check(unified=False)
