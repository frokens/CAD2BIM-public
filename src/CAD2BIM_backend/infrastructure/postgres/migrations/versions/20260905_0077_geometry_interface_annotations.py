"""Allow exact fixed-model interface annotation Operations.

Revision ID: 20260905_0077
Revises: 20260905_0076
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260905_0077"
down_revision: str | None = "20260905_0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "critical_operations"
_DETAIL_CHECK = "ck_critical_operations_operation_kind_detail_valid"
_ANNOTATION_KIND = "geometry_interface_annotation"


def _detail_check(*, annotation: bool) -> str:
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
    return " OR ".join((source, transform, ifc, geometry, family))


def _replace_detail_check(*, annotation: bool) -> None:
    op.drop_constraint(op.f(_DETAIL_CHECK), _TABLE, type_="check")
    op.create_check_constraint(
        op.f(_DETAIL_CHECK),
        _TABLE,
        _detail_check(annotation=annotation),
    )


def _install_guard(*, annotation: bool) -> None:
    annotation_branch = (
        """
                IF NEW.operation_detail_json ->> 'detail_kind' =
                   'geometry_interface_annotation' THEN
                    IF NEW.code_artifact_id IS NOT NULL
                       OR NEW.code_artifact_version_id IS NOT NULL
                       OR NEW.code_artifact_sha256 IS NOT NULL
                       OR NEW.repair_parent_operation_id IS NOT NULL THEN
                        RAISE EXCEPTION 'geometry interface annotation cannot contain source or repair bindings'
                            USING ERRCODE = '23514';
                    END IF;
                    SELECT resource.owner_scope, resource.owner_tenant_id,
                           resource.owner_task_id, resource.display_name
                      INTO base_scope, base_tenant_id, base_task_id, base_display_name
                      FROM geometry_resource_versions AS version
                      JOIN geometry_resources AS resource
                        ON resource.resource_id = version.resource_id
                     WHERE resource.resource_id =
                               NEW.operation_detail_json ->> 'base_resource_id'
                       AND version.resource_version_id =
                               NEW.operation_detail_json ->> 'base_resource_version_id'
                       AND version.content_digest =
                               NEW.operation_detail_json ->> 'base_content_digest'
                       AND resource.resource_kind = 'mapped_model';
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'geometry interface annotation exact mapped base is invalid'
                            USING ERRCODE = '23503';
                    END IF;
                    IF NEW.operation_detail_json ->> 'target_mode' = 'same_task_root' THEN
                        IF base_scope <> 'TASK'
                           OR base_tenant_id IS DISTINCT FROM NEW.tenant_id
                           OR base_task_id IS DISTINCT FROM NEW.task_id
                           OR base_display_name IS DISTINCT FROM
                                  NEW.operation_detail_json ->> 'display_name' THEN
                            RAISE EXCEPTION 'same-root annotation base is not writable in this Task'
                                USING ERRCODE = '23514';
                        END IF;
                    ELSE
                        IF NOT (
                            (base_scope = 'SYSTEM'
                             AND base_tenant_id IS NULL AND base_task_id IS NULL) OR
                            (base_scope = 'TENANT'
                             AND base_tenant_id = NEW.tenant_id AND base_task_id IS NULL) OR
                            (base_scope = 'TASK'
                             AND base_tenant_id = NEW.tenant_id
                             AND base_task_id = NEW.task_id)
                        ) THEN
                            RAISE EXCEPTION 'derived annotation base is not visible in this Task'
                                USING ERRCODE = '23514';
                        END IF;
                        IF TG_OP = 'INSERT' AND EXISTS (
                            SELECT 1 FROM geometry_resources AS target
                             WHERE target.resource_id = NEW.target_geometry_resource_id
                        ) THEN
                            RAISE EXCEPTION 'derived annotation target root already exists'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;
                    IF NEW.status = 'succeeded' AND NOT EXISTS (
                        SELECT 1
                          FROM geometry_resource_versions AS result_version
                          JOIN geometry_resources AS result_resource
                            ON result_resource.resource_id = result_version.resource_id
                         WHERE result_version.resource_version_id =
                                   NEW.result_geometry_resource_version_id
                           AND result_version.resource_id = NEW.target_geometry_resource_id
                           AND result_version.origin_kind = 'interface_annotation'
                           AND result_resource.resource_kind = 'mapped_model'
                           AND result_resource.owner_scope = 'TASK'
                           AND result_resource.owner_tenant_id = NEW.tenant_id
                           AND result_resource.owner_task_id = NEW.task_id
                           AND result_resource.display_name =
                                   NEW.operation_detail_json ->> 'display_name'
                           AND COALESCE(result_version.manifest_json -> 'interfaces', '[]'::jsonb) =
                                   NEW.operation_detail_json -> 'interfaces'
                           AND (result_version.manifest_json - 'interfaces') =
                               ((SELECT manifest_json FROM geometry_resource_versions
                                  WHERE resource_version_id =
                                      NEW.operation_detail_json ->> 'base_resource_version_id')
                                - 'interfaces')
                           AND result_version.validation_summary_json =
                               (SELECT validation_summary_json
                                  FROM geometry_resource_versions
                                 WHERE resource_version_id =
                                      NEW.operation_detail_json ->> 'base_resource_version_id')
                    ) THEN
                        RAISE EXCEPTION 'geometry interface annotation result metadata is invalid'
                            USING ERRCODE = '23514';
                    END IF;
                    IF NEW.status = 'succeeded' AND (
                        EXISTS (
                            SELECT asset_role, asset_format, filename, content_type,
                                   size_bytes, sha256
                              FROM geometry_resource_version_assets
                             WHERE resource_version_id =
                                      NEW.operation_detail_json ->> 'base_resource_version_id'
                               AND asset_role <> 'manifest'
                            EXCEPT
                            SELECT asset_role, asset_format, filename, content_type,
                                   size_bytes, sha256
                              FROM geometry_resource_version_assets
                             WHERE resource_version_id =
                                      NEW.result_geometry_resource_version_id
                               AND asset_role <> 'manifest'
                        ) OR EXISTS (
                            SELECT asset_role, asset_format, filename, content_type,
                                   size_bytes, sha256
                              FROM geometry_resource_version_assets
                             WHERE resource_version_id =
                                      NEW.result_geometry_resource_version_id
                               AND asset_role <> 'manifest'
                            EXCEPT
                            SELECT asset_role, asset_format, filename, content_type,
                                   size_bytes, sha256
                              FROM geometry_resource_version_assets
                             WHERE resource_version_id =
                                      NEW.operation_detail_json ->> 'base_resource_version_id'
                               AND asset_role <> 'manifest'
                        )
                    ) THEN
                        RAISE EXCEPTION 'geometry interface annotation changed immutable base assets'
                            USING ERRCODE = '23514';
                    END IF;
                ELSE
        """
        if annotation
        else ""
    )
    annotation_end = "                END IF;" if annotation else ""
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION cad2bim_guard_geometry_authoring_operation()
            RETURNS trigger LANGUAGE plpgsql AS $guard$
            DECLARE
                base_scope text;
                base_tenant_id text;
                base_task_id text;
                base_display_name text;
            BEGIN
                IF NEW.operation_kind <> 'geometry_resource_write' THEN
                    RETURN NEW;
                END IF;
                IF TG_OP = 'INSERT' THEN
                    IF NEW.status <> 'prepared' THEN
                        RAISE EXCEPTION 'geometry authoring Operation must be inserted prepared'
                            USING ERRCODE = '23514';
                    END IF;
                ELSE
                    IF NOT ((OLD.status = 'prepared' AND
                             NEW.status IN ('succeeded', 'failed', 'indeterminate')) OR
                            (OLD.status = 'indeterminate' AND NEW.status = 'prepared')) THEN
                        RAISE EXCEPTION 'geometry authoring terminal state is immutable'
                            USING ERRCODE = '23514';
                    END IF;
                    IF ROW(
                        NEW.operation_id, NEW.owner_id, NEW.tenant_id, NEW.task_id,
                        NEW.run_id, NEW.operation_kind, NEW.initiator, NEW.tool_name,
                        NEW.intent_summary, NEW.idempotency_key, NEW.request_fingerprint,
                        NEW.code_artifact_id, NEW.code_artifact_version_id,
                        NEW.code_artifact_sha256, NEW.repair_parent_operation_id,
                        NEW.target_geometry_resource_id, NEW.operation_detail_json,
                        NEW.started_at, NEW.created_at
                    ) IS DISTINCT FROM ROW(
                        OLD.operation_id, OLD.owner_id, OLD.tenant_id, OLD.task_id,
                        OLD.run_id, OLD.operation_kind, OLD.initiator, OLD.tool_name,
                        OLD.intent_summary, OLD.idempotency_key, OLD.request_fingerprint,
                        OLD.code_artifact_id, OLD.code_artifact_version_id,
                        OLD.code_artifact_sha256, OLD.repair_parent_operation_id,
                        OLD.target_geometry_resource_id, OLD.operation_detail_json,
                        OLD.started_at, OLD.created_at
                    ) THEN
                        RAISE EXCEPTION 'geometry authoring Operation identity is immutable'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;
{annotation_branch}
                    IF NOT EXISTS (
                        SELECT 1 FROM artifact_versions AS source_version
                        JOIN artifacts AS source_artifact
                          ON source_artifact.artifact_id = source_version.artifact_id
                         AND source_artifact.owner_id = source_version.owner_id
                         AND source_artifact.task_id = source_version.task_id
                         WHERE source_version.owner_id = NEW.owner_id
                           AND source_version.tenant_id = NEW.tenant_id
                           AND source_version.task_id = NEW.task_id
                           AND source_version.artifact_id = NEW.code_artifact_id
                           AND source_version.version_id = NEW.code_artifact_version_id
                           AND source_version.sha256 = NEW.code_artifact_sha256
                           AND source_artifact.kind = 'geometry_authoring_source'
                           AND source_artifact.status IN ('ready', 'available')
                    ) THEN
                        RAISE EXCEPTION 'geometry authoring exact source ArtifactVersion is invalid'
                            USING ERRCODE = '23503';
                    END IF;
                    IF NEW.repair_parent_operation_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM critical_operations AS parent
                         WHERE parent.operation_id = NEW.repair_parent_operation_id
                           AND parent.owner_id = NEW.owner_id
                           AND parent.tenant_id = NEW.tenant_id
                           AND parent.task_id = NEW.task_id
                           AND parent.run_id = NEW.run_id
                           AND parent.operation_kind = 'geometry_resource_write'
                           AND parent.status IN ('succeeded', 'failed')
                           AND parent.target_geometry_resource_id =
                               NEW.target_geometry_resource_id
                           AND parent.code_artifact_id IS NOT NULL
                           AND parent.code_artifact_version_id IS NOT NULL
                           AND parent.code_artifact_sha256 IS NOT NULL
                    ) THEN
                        RAISE EXCEPTION 'geometry authoring repair parent is not an exact terminal chain parent'
                            USING ERRCODE = '23503';
                    END IF;
{annotation_end}
                RETURN NEW;
            END
            $guard$
            """
        )
    )


def upgrade() -> None:
    _replace_detail_check(annotation=True)
    _install_guard(annotation=True)


def downgrade() -> None:
    op.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM {_TABLE}
                     WHERE operation_kind = 'geometry_resource_write'
                       AND operation_detail_json ->> 'detail_kind' = '{_ANNOTATION_KIND}'
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'geometry interface annotation Operations exist';
                END IF;
            END
            $$
            """
        )
    )
    _replace_detail_check(annotation=False)
    _install_guard(annotation=False)
