"""Allow durable ordered product writes without changing other Operation contracts."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260909_0081"
down_revision = "20260909_0080"
branch_labels = None
depends_on = None

_TABLE = "critical_operations"
_DETAIL_CHECK = "ck_critical_operations_operation_kind_detail_valid"

def _detail_check(*, products: bool) -> str:
    annotation = True
    source = (
        "(operation_kind = 'source_ingestion' AND tool_name = 'cad_import_source' "
        "AND source_filename IS NOT NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NULL AND code_artifact_version_id IS NULL "
        "AND code_artifact_sha256 IS NULL AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL AND repair_parent_operation_id IS NULL "
        "AND base_ifc_revision_id IS NULL AND expected_ifc_write_epoch IS NULL "
        "AND target_geometry_resource_id IS NULL "
        "AND result_geometry_resource_version_id IS NULL "
        "AND target_product_family_id IS NULL AND base_product_family_version_id IS NULL "
        "AND result_product_family_version_id IS NULL)"
    )
    transform = (
        "(operation_kind = 'data_transform' "
        "AND tool_name IN ('data_transform', 'data_repair_transform', "
        "'cad_compute_measurements') AND source_filename IS NULL "
        "AND intent_summary IS NOT NULL AND code_artifact_id IS NOT NULL "
        "AND code_artifact_version_id IS NOT NULL AND code_artifact_sha256 IS NOT NULL "
        "AND ((tool_name = 'cad_compute_measurements' AND repair_parent_operation_id IS NULL) "
        "OR (tool_name = 'data_repair_transform' "
        "AND description_artifact_id IS NOT NULL "
        "AND description_artifact_version_id IS NOT NULL "
        "AND description_artifact_sha256 IS NOT NULL "
        "AND repair_parent_operation_id IS NOT NULL) OR "
        "(tool_name = 'data_transform' AND (repair_parent_operation_id IS NULL OR "
        "(repair_parent_operation_id IS NOT NULL AND description_artifact_id IS NOT NULL "
        "AND description_artifact_version_id IS NOT NULL "
        "AND description_artifact_sha256 IS NOT NULL)))) "
        "AND base_ifc_revision_id IS NULL AND expected_ifc_write_epoch IS NULL "
        "AND target_geometry_resource_id IS NULL "
        "AND result_geometry_resource_version_id IS NULL "
        "AND target_product_family_id IS NULL AND base_product_family_version_id IS NULL "
        "AND result_product_family_version_id IS NULL)"
    )
    ifc = (
        "(operation_kind = 'ifc_model_write' AND source_filename IS NULL "
        "AND intent_summary IS NOT NULL AND code_artifact_id IS NULL "
        "AND code_artifact_version_id IS NULL AND code_artifact_sha256 IS NULL "
        "AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL AND repair_parent_operation_id IS NULL "
        "AND result_package_id IS NULL AND target_geometry_resource_id IS NULL "
        "AND result_geometry_resource_version_id IS NULL "
        "AND target_product_family_id IS NULL AND base_product_family_version_id IS NULL "
        "AND result_product_family_version_id IS NULL "
        "AND ((tool_name = 'ifc_model_initialize' AND base_ifc_revision_id IS NULL "
        "AND expected_ifc_write_epoch IS NULL) OR "
        "(tool_name IN ('ifc_model_configure_geometry_basis', 'ifc_project_update', "
        "'ifc_spatial_structure_apply') AND base_ifc_revision_id IS NOT NULL "
        "AND expected_ifc_write_epoch IS NOT NULL)))"
    )
    annotation_exclusion = (
        "AND operation_detail_json ->> 'detail_kind' IS DISTINCT FROM "
        "'geometry_interface_annotation' "
        if annotation
        else ""
    )
    source_geometry = (
        "(code_artifact_id IS NOT NULL AND code_artifact_version_id IS NOT NULL "
        "AND code_artifact_sha256 IS NOT NULL "
        f"{annotation_exclusion}"
        "AND ((tool_name = 'ifc_geometry_resource_create' "
        "AND repair_parent_operation_id IS NULL) OR "
        "(tool_name = 'ifc_geometry_resource_repair' "
        "AND repair_parent_operation_id IS NOT NULL) OR "
        "tool_name = 'ifc_geometry_resource_write'))"
    )
    annotation_geometry = (
        " OR (tool_name = 'ifc_geometry_resource_write' "
        "AND code_artifact_id IS NULL AND code_artifact_version_id IS NULL "
        "AND code_artifact_sha256 IS NULL AND repair_parent_operation_id IS NULL "
        "AND operation_detail_json ->> 'detail_kind' = 'geometry_interface_annotation' "
        "AND operation_detail_json ->> 'resource_kind' = 'mapped_model' "
        "AND operation_detail_json ->> 'target_mode' IN "
        "('same_task_root', 'derive_task_root') "
        "AND jsonb_typeof(operation_detail_json -> 'interfaces') = 'array' "
        "AND jsonb_array_length(operation_detail_json -> 'interfaces') <= 64 "
        "AND length(operation_detail_json ->> 'base_resource_id') BETWEEN 1 AND 80 "
        "AND length(operation_detail_json ->> 'base_resource_version_id') BETWEEN 1 AND 80 "
        "AND operation_detail_json ->> 'base_content_digest' ~ '^[0-9a-f]{64}$' "
        "AND length(operation_detail_json ->> 'display_name') BETWEEN 1 AND 255 "
        "AND operation_detail_json ->> 'display_name' = "
        "trim(operation_detail_json ->> 'display_name') "
        "AND ((operation_detail_json ->> 'target_mode' = 'same_task_root' "
        "AND target_geometry_resource_id = operation_detail_json ->> 'base_resource_id') OR "
        "(operation_detail_json ->> 'target_mode' = 'derive_task_root' "
        "AND target_geometry_resource_id <> operation_detail_json ->> 'base_resource_id')))"
        if annotation
        else ""
    )
    geometry = (
        "(operation_kind = 'geometry_resource_write' "
        "AND tool_name IN ('ifc_geometry_resource_create', "
        "'ifc_geometry_resource_repair', 'ifc_geometry_resource_write') "
        "AND source_filename IS NULL AND intent_summary IS NOT NULL "
        "AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        f"AND COALESCE(({source_geometry}{annotation_geometry}), FALSE) "
        "AND result_package_id IS NULL AND base_ifc_revision_id IS NULL "
        "AND expected_ifc_write_epoch IS NULL "
        "AND target_geometry_resource_id IS NOT NULL "
        "AND target_product_family_id IS NULL AND base_product_family_version_id IS NULL "
        "AND result_product_family_version_id IS NULL)"
    )
    family = (
        "(COALESCE((operation_kind = 'product_family_write' "
        "AND tool_name = 'ifc_product_family_write' "
        "AND source_filename IS NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NOT NULL AND code_artifact_version_id IS NOT NULL "
        "AND code_artifact_sha256 IS NOT NULL "
        "AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        "AND result_package_id IS NULL AND base_ifc_revision_id IS NULL "
        "AND expected_ifc_write_epoch IS NULL "
        "AND target_geometry_resource_id IS NULL "
        "AND result_geometry_resource_version_id IS NULL "
        "AND target_product_family_id IS NOT NULL "
        "AND operation_detail_json -> 'version_draft' IS NOT NULL "
        "AND jsonb_typeof(operation_detail_json -> 'version_draft') = 'object' "
        "AND operation_detail_json #>> '{version_draft,abi_profile}' = "
        "'product_realization_v1' "
        "AND operation_detail_json ->> 'detail_kind' = 'product_family_write' "
        "AND operation_detail_json ->> 'mode' IN "
        "('create', 'repair_attempt', 'revise_version') "
        "AND ((operation_detail_json ->> 'mode' = 'create' "
        "AND repair_parent_operation_id IS NULL "
        "AND base_product_family_version_id IS NULL) OR "
        "(operation_detail_json ->> 'mode' = 'repair_attempt' "
        "AND repair_parent_operation_id IS NOT NULL "
        "AND base_product_family_version_id IS NOT DISTINCT FROM "
        "operation_detail_json #>> '{version_draft,parent_family_version_id}') OR "
        "(operation_detail_json ->> 'mode' = 'revise_version' "
        "AND repair_parent_operation_id IS NULL "
        "AND base_product_family_version_id IS NOT NULL "
        "AND base_product_family_version_id = operation_detail_json #>> "
        "'{version_draft,parent_family_version_id}'))), FALSE))"
    )
    if products:
        ifc = ifc.replace("\'ifc_spatial_structure_apply\')", "\'ifc_spatial_structure_apply\', \'ifc_products_apply\')")
    return " OR ".join((source, transform, ifc, geometry, family))

def _replace_detail_check(*, products: bool) -> None:
    op.drop_constraint(op.f(_DETAIL_CHECK), _TABLE, type_="check")
    op.create_check_constraint(op.f(_DETAIL_CHECK), _TABLE, _detail_check(products=products))


def upgrade() -> None:
    _replace_detail_check(products=True)


def downgrade() -> None:
    op.execute(sa.text("LOCK TABLE critical_operations IN ACCESS EXCLUSIVE MODE"))
    op.execute(sa.text("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM critical_operations WHERE tool_name = 'ifc_products_apply') THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
          MESSAGE = 'product apply Operations exist; downgrade would discard their contract';
      END IF;
    END $$
    """))
    _replace_detail_check(products=False)

