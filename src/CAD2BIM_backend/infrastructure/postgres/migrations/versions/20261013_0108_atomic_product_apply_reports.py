"""Publish strict product reports with the IFC Operation.

Revision ID: 20261013_0108
Revises: 20261006_0107
"""
from alembic import op
import sqlalchemy as sa

revision = "20261013_0108"
down_revision = "20261006_0107"
branch_labels = None
depends_on = None

_CHECKS = {'ck_critical_operations_operation_kind_detail_valid': "(operation_kind = 'source_ingestion' "
                                                       "AND tool_name = 'cad_import_source' "
                                                       'AND source_filename IS NOT NULL AND '
                                                       'intent_summary IS NOT NULL AND '
                                                       'code_artifact_id IS NULL AND '
                                                       'code_artifact_version_id IS NULL AND '
                                                       'code_artifact_sha256 IS NULL AND '
                                                       'description_artifact_id IS NULL AND '
                                                       'description_artifact_version_id IS '
                                                       'NULL AND description_artifact_sha256 '
                                                       'IS NULL AND '
                                                       'repair_parent_operation_id IS NULL '
                                                       'AND base_ifc_revision_id IS NULL AND '
                                                       'expected_ifc_write_epoch IS NULL AND '
                                                       'target_geometry_resource_id IS NULL '
                                                       'AND '
                                                       'result_geometry_resource_version_id '
                                                       'IS NULL AND target_product_family_id '
                                                       'IS NULL AND '
                                                       'base_product_family_version_id IS '
                                                       'NULL AND '
                                                       'result_product_family_version_id IS '
                                                       'NULL) OR (operation_kind = '
                                                       "'data_transform' AND tool_name IN "
                                                       "('data_transform', "
                                                       "'data_repair_transform', "
                                                       "'cad_compute_measurements') AND "
                                                       'source_filename IS NULL AND '
                                                       'intent_summary IS NOT NULL AND '
                                                       'code_artifact_id IS NOT NULL AND '
                                                       'code_artifact_version_id IS NOT NULL '
                                                       'AND code_artifact_sha256 IS NOT NULL '
                                                       'AND ((tool_name = '
                                                       "'cad_compute_measurements' AND "
                                                       'repair_parent_operation_id IS NULL) '
                                                       'OR (tool_name = '
                                                       "'data_repair_transform' AND "
                                                       'description_artifact_id IS NOT NULL '
                                                       'AND description_artifact_version_id '
                                                       'IS NOT NULL AND '
                                                       'description_artifact_sha256 IS NOT '
                                                       'NULL AND repair_parent_operation_id '
                                                       'IS NOT NULL) OR (tool_name = '
                                                       "'data_transform' AND "
                                                       '(repair_parent_operation_id IS NULL '
                                                       'OR (repair_parent_operation_id IS NOT '
                                                       'NULL AND description_artifact_id IS '
                                                       'NOT NULL AND '
                                                       'description_artifact_version_id IS '
                                                       'NOT NULL AND '
                                                       'description_artifact_sha256 IS NOT '
                                                       'NULL)))) AND base_ifc_revision_id IS '
                                                       'NULL AND expected_ifc_write_epoch IS '
                                                       'NULL AND target_geometry_resource_id '
                                                       'IS NULL AND '
                                                       'result_geometry_resource_version_id '
                                                       'IS NULL AND target_product_family_id '
                                                       'IS NULL AND '
                                                       'base_product_family_version_id IS '
                                                       'NULL AND '
                                                       'result_product_family_version_id IS '
                                                       'NULL) OR (operation_kind = '
                                                       "'ifc_model_write' AND source_filename "
                                                       'IS NULL AND intent_summary IS NOT '
                                                       'NULL AND code_artifact_id IS NULL AND '
                                                       'code_artifact_version_id IS NULL AND '
                                                       'code_artifact_sha256 IS NULL AND '
                                                       'description_artifact_id IS NULL AND '
                                                       'description_artifact_version_id IS '
                                                       'NULL AND description_artifact_sha256 '
                                                       'IS NULL AND '
                                                       'repair_parent_operation_id IS NULL '
                                                       'AND (result_package_id IS NULL OR '
                                                       "tool_name='ifc_products_apply') AND "
                                                       'target_geometry_resource_id IS NULL '
                                                       'AND '
                                                       'result_geometry_resource_version_id '
                                                       'IS NULL AND target_product_family_id '
                                                       'IS NULL AND '
                                                       'base_product_family_version_id IS '
                                                       'NULL AND '
                                                       'result_product_family_version_id IS '
                                                       'NULL AND ((tool_name = '
                                                       "'ifc_model_initialize' AND "
                                                       'base_ifc_revision_id IS NULL AND '
                                                       'expected_ifc_write_epoch IS NULL) OR '
                                                       '(tool_name IN '
                                                       "('ifc_model_configure_geometry_basis', "
                                                       "'ifc_project_update', "
                                                       "'ifc_spatial_structure_apply', "
                                                       "'ifc_products_apply') AND "
                                                       'base_ifc_revision_id IS NOT NULL AND '
                                                       'expected_ifc_write_epoch IS NOT '
                                                       'NULL))) OR (operation_kind = '
                                                       "'geometry_resource_write' AND "
                                                       'tool_name IN '
                                                       "('ifc_geometry_resource_create', "
                                                       "'ifc_geometry_resource_repair', "
                                                       "'ifc_geometry_resource_write') AND "
                                                       'source_filename IS NULL AND '
                                                       'intent_summary IS NOT NULL AND '
                                                       'description_artifact_id IS NULL AND '
                                                       'description_artifact_version_id IS '
                                                       'NULL AND description_artifact_sha256 '
                                                       'IS NULL AND '
                                                       'COALESCE(((code_artifact_id IS NOT '
                                                       'NULL AND code_artifact_version_id IS '
                                                       'NOT NULL AND code_artifact_sha256 IS '
                                                       'NOT NULL AND operation_detail_json '
                                                       "->> 'detail_kind' IS DISTINCT FROM "
                                                       "'geometry_interface_annotation' AND "
                                                       '((tool_name = '
                                                       "'ifc_geometry_resource_create' AND "
                                                       'repair_parent_operation_id IS NULL) '
                                                       'OR (tool_name = '
                                                       "'ifc_geometry_resource_repair' AND "
                                                       'repair_parent_operation_id IS NOT '
                                                       'NULL) OR tool_name = '
                                                       "'ifc_geometry_resource_write')) OR "
                                                       '(tool_name = '
                                                       "'ifc_geometry_resource_write' AND "
                                                       'code_artifact_id IS NULL AND '
                                                       'code_artifact_version_id IS NULL AND '
                                                       'code_artifact_sha256 IS NULL AND '
                                                       'repair_parent_operation_id IS NULL '
                                                       'AND operation_detail_json ->> '
                                                       "'detail_kind' = "
                                                       "'geometry_interface_annotation' AND "
                                                       'operation_detail_json ->> '
                                                       "'resource_kind' = 'mapped_model' AND "
                                                       'operation_detail_json ->> '
                                                       "'target_mode' IN ('same_task_root', "
                                                       "'derive_task_root') AND "
                                                       'jsonb_typeof(operation_detail_json -> '
                                                       "'interfaces') = 'array' AND "
                                                       'jsonb_array_length(operation_detail_json '
                                                       "-> 'interfaces') <= 64 AND "
                                                       'length(operation_detail_json ->> '
                                                       "'base_resource_id') BETWEEN 1 AND 80 "
                                                       'AND length(operation_detail_json ->> '
                                                       "'base_resource_version_id') BETWEEN 1 "
                                                       'AND 80 AND operation_detail_json ->> '
                                                       "'base_content_digest' ~ "
                                                       "'^[0-9a-f]{64}$' AND "
                                                       'length(operation_detail_json ->> '
                                                       "'display_name') BETWEEN 1 AND 255 AND "
                                                       'operation_detail_json ->> '
                                                       "'display_name' = "
                                                       'trim(operation_detail_json ->> '
                                                       "'display_name') AND "
                                                       '((operation_detail_json ->> '
                                                       "'target_mode' = 'same_task_root' AND "
                                                       'target_geometry_resource_id = '
                                                       'operation_detail_json ->> '
                                                       "'base_resource_id') OR "
                                                       '(operation_detail_json ->> '
                                                       "'target_mode' = 'derive_task_root' "
                                                       'AND target_geometry_resource_id <> '
                                                       'operation_detail_json ->> '
                                                       "'base_resource_id')))), FALSE) AND "
                                                       'result_package_id IS NULL AND '
                                                       'base_ifc_revision_id IS NULL AND '
                                                       'expected_ifc_write_epoch IS NULL AND '
                                                       'target_geometry_resource_id IS NOT '
                                                       'NULL AND target_product_family_id IS '
                                                       'NULL AND '
                                                       'base_product_family_version_id IS '
                                                       'NULL AND '
                                                       'result_product_family_version_id IS '
                                                       'NULL) OR (COALESCE((operation_kind = '
                                                       "'product_family_write' AND tool_name "
                                                       "= 'ifc_product_family_write' AND "
                                                       'source_filename IS NULL AND '
                                                       'intent_summary IS NOT NULL AND '
                                                       'code_artifact_id IS NOT NULL AND '
                                                       'code_artifact_version_id IS NOT NULL '
                                                       'AND code_artifact_sha256 IS NOT NULL '
                                                       'AND description_artifact_id IS NULL '
                                                       'AND description_artifact_version_id '
                                                       'IS NULL AND '
                                                       'description_artifact_sha256 IS NULL '
                                                       'AND result_package_id IS NULL AND '
                                                       'base_ifc_revision_id IS NULL AND '
                                                       'expected_ifc_write_epoch IS NULL AND '
                                                       'target_geometry_resource_id IS NULL '
                                                       'AND '
                                                       'result_geometry_resource_version_id '
                                                       'IS NULL AND target_product_family_id '
                                                       'IS NOT NULL AND operation_detail_json '
                                                       "-> 'version_draft' IS NOT NULL AND "
                                                       'jsonb_typeof(operation_detail_json -> '
                                                       "'version_draft') = 'object' AND "
                                                       'operation_detail_json ->> '
                                                       "'detail_kind' = "
                                                       "'product_family_write' AND "
                                                       "operation_detail_json ->> 'mode' IN "
                                                       "('create', 'repair_attempt', "
                                                       "'revise_version', 'system_copy') AND "
                                                       "((operation_detail_json ->> 'mode' IN "
                                                       "('create', 'system_copy') AND "
                                                       'repair_parent_operation_id IS NULL '
                                                       'AND base_product_family_version_id IS '
                                                       'NULL) OR (operation_detail_json ->> '
                                                       "'mode' = 'repair_attempt' AND "
                                                       'repair_parent_operation_id IS NOT '
                                                       'NULL AND '
                                                       'base_product_family_version_id IS NOT '
                                                       'DISTINCT FROM operation_detail_json '
                                                       '#>> '
                                                       "'{version_draft,parent_family_version_id}') "
                                                       "OR (operation_detail_json ->> 'mode' "
                                                       "= 'revise_version' AND "
                                                       'repair_parent_operation_id IS NULL '
                                                       'AND base_product_family_version_id IS '
                                                       'NOT NULL AND '
                                                       'base_product_family_version_id = '
                                                       'operation_detail_json #>> '
                                                       "'{version_draft,parent_family_version_id}'))), "
                                                       'FALSE)) OR (operation_kind = '
                                                       "'geometry_inspection') OR "
                                                       '(operation_kind IN '
                                                       "('geometry_resource_write','product_family_write') "
                                                       'AND operation_detail_json ->> '
                                                       "'detail_kind' = "
                                                       "'authoring_draft_write' AND "
                                                       'code_artifact_version_id IS NOT NULL '
                                                       'AND '
                                                       'result_geometry_resource_version_id '
                                                       'IS NULL AND '
                                                       'result_product_family_version_id IS '
                                                       'NULL AND operation_detail_json ->> '
                                                       "'root_id' = "
                                                       'COALESCE(target_geometry_resource_id,target_product_family_id) '
                                                       'AND (operation_detail_json ->> '
                                                       "'result_generation')::bigint = "
                                                       'COALESCE((operation_detail_json ->> '
                                                       "'expected_generation')::bigint,0)+1) "
                                                       'OR '
                                                       "(operation_kind='authoring_publish' "
                                                       "AND tool_name='ifc_authoring_publish' "
                                                       'AND operation_detail_json ->> '
                                                       "'detail_kind'='authoring_publish' AND "
                                                       'jsonb_typeof(operation_detail_json -> '
                                                       "'preview_examples')='array' AND "
                                                       'jsonb_array_length(operation_detail_json '
                                                       "-> 'expected_drafts')>0 AND NOT "
                                                       "(operation_detail_json ? 'evidence') "
                                                       'AND NOT (operation_detail_json ? '
                                                       "'evidence_run_id'))",
 'ck_critical_operations_products_apply_detail_valid': "tool_name<>'ifc_products_apply' OR "
                                                       "COALESCE((operation_kind='ifc_model_write' "
                                                       'AND operation_detail_json ->> '
                                                       "'detail_kind'='ifc_products_apply' "
                                                       'AND operation_detail_json -> '
                                                       "'request' ->> 'family_version_id' IS "
                                                       'NOT NULL AND operation_detail_json -> '
                                                       "'input_binding' ->> 'member_id' IS "
                                                       'NOT NULL AND operation_detail_json -> '
                                                       "'request' ->> 'package_id' = "
                                                       'operation_detail_json -> '
                                                       "'input_binding' ->> 'package_id' AND "
                                                       "operation_detail_json -> 'request' "
                                                       "->> 'payload_name' = "
                                                       'operation_detail_json -> '
                                                       "'input_binding' ->> 'payload_name' "
                                                       'AND ((status IN '
                                                       "('prepared','indeterminate') AND "
                                                       "(operation_detail_json -> 'result' IS "
                                                       'NULL OR operation_detail_json -> '
                                                       "'result'='null'::jsonb)) OR "
                                                       "(status='succeeded' AND "
                                                       'result_package_id IS NOT NULL AND '
                                                       "operation_detail_json -> 'result' ->> "
                                                       "'outcome'='committed') OR "
                                                       "(status='failed' AND "
                                                       '((result_package_id IS NULL AND '
                                                       "(operation_detail_json -> 'result' IS "
                                                       'NULL OR operation_detail_json -> '
                                                       "'result'='null'::jsonb)) OR "
                                                       '(result_package_id IS NOT NULL AND '
                                                       "operation_detail_json -> 'result' ->> "
                                                       "'outcome' IN "
                                                       "('rejected','technical_failure')))))), "
                                                       'FALSE)',
 'ck_critical_operations_terminal_result_valid': "(status = 'prepared' AND finished_at IS "
                                                 'NULL AND result_package_id IS NULL AND '
                                                 'error_code IS NULL AND error_message IS '
                                                 'NULL AND error_phase IS NULL AND '
                                                 'result_geometry_resource_version_id IS NULL '
                                                 'AND result_product_family_version_id IS '
                                                 "NULL) OR (status = 'succeeded' AND "
                                                 'finished_at IS NOT NULL AND error_code IS '
                                                 'NULL AND error_message IS NULL AND '
                                                 'error_phase IS NULL AND ((operation_kind IN '
                                                 "('source_ingestion', 'data_transform') AND "
                                                 'result_package_id IS NOT NULL AND '
                                                 'result_geometry_resource_version_id IS '
                                                 'NULL) OR (operation_kind = '
                                                 "'ifc_model_write' AND "
                                                 "((tool_name<>'ifc_products_apply' AND "
                                                 'result_package_id IS NULL) OR '
                                                 "(tool_name='ifc_products_apply' AND "
                                                 'result_package_id IS NOT NULL)) AND '
                                                 'result_geometry_resource_version_id IS '
                                                 'NULL) OR (operation_kind = '
                                                 "'geometry_resource_write' AND "
                                                 'result_package_id IS NULL AND '
                                                 'result_geometry_resource_version_id IS NOT '
                                                 'NULL AND result_product_family_version_id '
                                                 'IS NULL) OR (operation_kind = '
                                                 "'product_family_write' AND "
                                                 'result_package_id IS NULL AND '
                                                 'result_geometry_resource_version_id IS NULL '
                                                 'AND result_product_family_version_id IS NOT '
                                                 'NULL) OR (operation_kind = '
                                                 "'geometry_inspection' AND result_package_id "
                                                 'IS NULL AND '
                                                 'result_geometry_resource_version_id IS NULL '
                                                 'AND result_product_family_version_id IS '
                                                 'NULL AND '
                                                 'inspection_result_artifact_version_id IS '
                                                 "NOT NULL))) OR (status = 'failed' AND "
                                                 'finished_at IS NOT NULL AND error_code IS '
                                                 'NOT NULL AND error_message IS NOT NULL AND '
                                                 '(result_package_id IS NULL OR '
                                                 "(operation_kind='ifc_model_write' AND "
                                                 "tool_name='ifc_products_apply')) AND "
                                                 'result_geometry_resource_version_id IS NULL '
                                                 'AND result_product_family_version_id IS '
                                                 "NULL) OR (status = 'indeterminate' AND "
                                                 'finished_at IS NOT NULL AND error_code IS '
                                                 'NOT NULL AND error_message IS NOT NULL AND '
                                                 "error_phase = 'reconcile' AND "
                                                 'result_package_id IS NULL AND '
                                                 'result_geometry_resource_version_id IS NULL '
                                                 'AND result_product_family_version_id IS '
                                                 "NULL) OR (status='succeeded' AND "
                                                 'finished_at IS NOT NULL AND error_code IS '
                                                 'NULL AND error_message IS NULL AND '
                                                 'error_phase IS NULL AND '
                                                 "(operation_kind='authoring_publish' OR "
                                                 'operation_detail_json ->> '
                                                 "'detail_kind'='authoring_draft_write'))"}


def upgrade():
    for name, condition in _CHECKS.items():
        was_validated = True
        if not name.endswith("products_apply_detail_valid"):
            was_validated = op.get_bind().scalar(sa.text(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conrelid='critical_operations'::regclass AND conname=:name"
            ), {"name": name})
            op.drop_constraint(op.f(name), "critical_operations", type_="check")
        if was_validated is False:
            # 0105 preserves immutable retired rows without admitting them to
            # current readers. Keep that existing validation boundary; every
            # new or updated row still has to satisfy the current constraint.
            op.execute(sa.text("ALTER TABLE critical_operations ADD CONSTRAINT "
                + name + " CHECK (" + condition + ") NOT VALID"))
        else:
            op.create_check_constraint(op.f(name), "critical_operations", condition)
    op.drop_constraint(op.f("ck_geometry_resource_version_assets_asset_role_valid"),
                       "geometry_resource_version_assets", type_="check")
    # Preserve retired immutable evidence; no current reader interprets that role.
    condition = "asset_role IN ('canonical_geometry', 'manifest', 'validation_report', 'source_bundle', 'parameter_schema', 'preview', 'inspection', 'sample_output')"
    count = op.get_bind().scalar(sa.text(
        "SELECT count(*) FROM geometry_resource_version_assets WHERE asset_role='runtime_manifest'"))
    if count:
        op.execute(sa.text("ALTER TABLE geometry_resource_version_assets ADD CONSTRAINT "
            "ck_geometry_resource_version_assets_asset_role_valid CHECK (" + condition + ") NOT VALID"))
    else:
        op.create_check_constraint(op.f("ck_geometry_resource_version_assets_asset_role_valid"),
                                   "geometry_resource_version_assets", condition)


def downgrade():
    raise RuntimeError("Restore the database snapshot; retired roles and Apply reports cannot be reinterpreted")
