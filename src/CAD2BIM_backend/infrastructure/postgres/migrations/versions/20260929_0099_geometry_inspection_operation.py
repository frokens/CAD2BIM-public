"""Publish retained Geometry/Family results through the existing Operation owner.

Revision ID: 20260929_0099
Revises: 20260929_0098
"""

from importlib import import_module

from alembic import op
import sqlalchemy as sa


revision = "20260929_0099"
down_revision = "20260929_0098"
branch_labels = None
depends_on = None

_TABLE = "critical_operations"
_PREFIX = "ck_critical_operations_"
_REFERENCE_COLUMNS = (
    "inspection_request_artifact_id", "inspection_request_artifact_version_id", "inspection_request_artifact_sha256",
    "inspection_result_artifact_id", "inspection_result_artifact_version_id", "inspection_result_artifact_sha256",
)
_SOURCE_COLUMNS = ("inspection_source_geometry_version_id", "inspection_source_family_version_id")
_CLAIM_SHAPE = """
    authoring_execution_generation >= 0 AND
    ((authoring_execution_owner IS NULL AND authoring_execution_token_sha256 IS NULL)
     OR (authoring_execution_owner IS NOT NULL AND authoring_execution_token_sha256 IS NOT NULL
         AND length(authoring_execution_token_sha256) = 64 AND authoring_execution_generation > 0))
"""
_DETAIL_SHAPE = """
    operation_kind <> 'geometry_inspection' OR COALESCE((
      jsonb_typeof(operation_detail_json) = 'object'
      AND operation_detail_json ->> 'detail_kind' = 'geometry_inspection'
      AND operation_detail_json - ARRAY['detail_kind','source_kind','source_id','source_version_id',
        'source_content_digest','source_version_ref','request_artifact'] = '{}'::jsonb
      AND length(operation_detail_json ->> 'source_id') BETWEEN 1 AND 80
      AND length(operation_detail_json ->> 'source_version_id') BETWEEN 1 AND 80
      AND operation_detail_json ->> 'source_content_digest' ~ '^[0-9a-f]{64}$'
      AND length(operation_detail_json ->> 'source_version_ref') BETWEEN 1 AND 16384
      AND jsonb_typeof(operation_detail_json -> 'request_artifact') = 'object'
      AND (operation_detail_json -> 'request_artifact') - ARRAY['artifact_id','version_id','sha256'] = '{}'::jsonb
      AND operation_detail_json #>> '{request_artifact,artifact_id}' = inspection_request_artifact_id
      AND operation_detail_json #>> '{request_artifact,version_id}' = inspection_request_artifact_version_id
      AND operation_detail_json #>> '{request_artifact,sha256}' = inspection_request_artifact_sha256
      AND ((operation_detail_json ->> 'source_kind' = 'geometry_sample'
            AND tool_name = 'ifc_geometry_preview' AND inspection_source_family_version_id IS NULL
            AND inspection_source_geometry_version_id = operation_detail_json ->> 'source_version_id')
        OR (operation_detail_json ->> 'source_kind' = 'family_realization'
            AND tool_name = 'ifc_product_realization_preview' AND inspection_source_geometry_version_id IS NULL
            AND inspection_source_family_version_id = operation_detail_json ->> 'source_version_id'))
    ), FALSE)
"""


def _previous(name):
    return import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions." + name)


def _replace_check(name, expression):
    op.drop_constraint(op.f(_PREFIX + name), _TABLE, type_="check")
    op.create_check_constraint(op.f(_PREFIX + name), _TABLE, expression)


def _checks(*, inspection):
    family = _previous("20260904_0074_product_family_authoring")
    detail = _previous("20260929_0097_system_family_publication")._detail_check(system_copy=True)
    kinds = "'source_ingestion','data_transform','ifc_model_write','geometry_resource_write','product_family_write'"
    authoring_kinds = "'geometry_resource_write','product_family_write'"
    terminal = family._terminal_check(product_family=True)
    phases = family._error_phase_check(product_family=True)
    if inspection:
        kinds += ",'geometry_inspection'"
        authoring_kinds += ",'geometry_inspection'"
        detail = "(" + detail + ") OR operation_kind = 'geometry_inspection'"
        terminal = "(" + terminal + ") OR (operation_kind = 'geometry_inspection' AND status = 'succeeded' " \
            "AND finished_at IS NOT NULL AND error_code IS NULL AND error_message IS NULL AND error_phase IS NULL " \
            "AND result_package_id IS NULL AND result_geometry_resource_version_id IS NULL " \
            "AND result_product_family_version_id IS NULL AND inspection_result_artifact_version_id IS NOT NULL)"
        phases += " OR (status = 'failed' AND operation_kind = 'geometry_inspection' AND error_phase IN " \
            "('code_validation','execution','output_validation','storage_verification','commit_fence'))"
    _replace_check("operation_kind_valid", f"operation_kind IN ({kinds})")
    _replace_check("operation_kind_detail_valid", detail)
    _replace_check("terminal_result_valid", terminal)
    _replace_check("error_phase_valid", phases)
    _replace_check("authoring_control_consistent",
        f"(operation_kind NOT IN ({authoring_kinds}) AND authoring_execution_owner IS NULL "
        "AND authoring_execution_generation = 0 AND authoring_execution_token_sha256 IS NULL) "
        f"OR (operation_kind IN ({authoring_kinds}) AND ({_CLAIM_SHAPE}))")
    op.drop_constraint(op.f("ck_opaque_reference_index_reference_kind_valid"), "opaque_reference_index", type_="check")
    reference_kinds = "'gr','gv','fr','fv','sc','gp','pp','gq'" + (",'gi'" if inspection else "")
    op.create_check_constraint(op.f("ck_opaque_reference_index_reference_kind_valid"), "opaque_reference_index",
        f"reference_kind IN ({reference_kinds})")


def _artifact_bindings():
    absent = " AND ".join(name + " IS NULL" for name in (*_REFERENCE_COLUMNS, *_SOURCE_COLUMNS))
    old_absent = " AND ".join(name + " IS NULL" for name in (
        "source_filename", "code_artifact_id", "code_artifact_version_id", "code_artifact_sha256",
        "description_artifact_id", "description_artifact_version_id", "description_artifact_sha256",
        "repair_parent_operation_id", "result_package_id", "base_ifc_revision_id", "expected_ifc_write_epoch",
        "target_geometry_resource_id", "result_geometry_resource_version_id", "target_product_family_id",
        "base_product_family_version_id", "result_product_family_version_id",
    ))
    request = " AND ".join(name + " IS NOT NULL" for name in _REFERENCE_COLUMNS[:3])
    result = " AND ".join(name + " IS NOT NULL" for name in _REFERENCE_COLUMNS[3:])
    no_result = " AND ".join(name + " IS NULL" for name in _REFERENCE_COLUMNS[3:])
    return f"(operation_kind <> 'geometry_inspection' AND {absent}) OR " \
        f"(operation_kind = 'geometry_inspection' AND tool_name IN ('ifc_geometry_preview','ifc_product_realization_preview') " \
        f"AND {old_absent} AND {request} AND ((status = 'succeeded' AND {result}) OR " \
        f"(status <> 'succeeded' AND {no_result})))"


def _install_guard():
    op.execute(sa.text("""
        CREATE FUNCTION cad2bim_guard_geometry_inspection_operation()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.operation_kind IS DISTINCT FROM NEW.operation_kind THEN
                RAISE EXCEPTION 'critical Operation kind is immutable' USING ERRCODE = '23514';
            END IF;
            IF NEW.operation_kind <> 'geometry_inspection' THEN RETURN NEW; END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'prepared' OR NEW.authoring_execution_owner IS NOT NULL
                   OR NEW.authoring_execution_generation <> 0 OR NEW.authoring_execution_token_sha256 IS NOT NULL THEN
                    RAISE EXCEPTION 'geometry inspection Operation must be admitted prepared and unclaimed' USING ERRCODE = '23514';
                END IF;
            ELSE
                IF to_jsonb(NEW) - ARRAY['status','inspection_result_artifact_id','inspection_result_artifact_version_id',
                    'inspection_result_artifact_sha256','error_code','error_message','error_phase','finished_at',
                    'authoring_execution_owner','authoring_execution_generation','authoring_execution_token_sha256']
                   IS DISTINCT FROM to_jsonb(OLD) - ARRAY['status','inspection_result_artifact_id','inspection_result_artifact_version_id',
                    'inspection_result_artifact_sha256','error_code','error_message','error_phase','finished_at',
                    'authoring_execution_owner','authoring_execution_generation','authoring_execution_token_sha256'] THEN
                    RAISE EXCEPTION 'geometry inspection Operation request is immutable' USING ERRCODE = '23514';
                END IF;
                IF OLD.status = NEW.status AND OLD.status IN ('prepared','indeterminate') THEN
                    IF to_jsonb(NEW) - ARRAY['authoring_execution_owner','authoring_execution_generation','authoring_execution_token_sha256']
                       IS DISTINCT FROM to_jsonb(OLD) - ARRAY['authoring_execution_owner','authoring_execution_generation','authoring_execution_token_sha256']
                       OR NEW.authoring_execution_owner IS NULL OR NEW.authoring_execution_token_sha256 IS NULL
                       OR NEW.authoring_execution_generation <> OLD.authoring_execution_generation + 1
                       OR NEW.authoring_execution_token_sha256 !~ '^[0-9a-f]{64}$' THEN
                        RAISE EXCEPTION 'geometry inspection claim update is invalid' USING ERRCODE = '23514';
                    END IF;
                ELSIF OLD.status NOT IN ('prepared','indeterminate') OR NEW.status NOT IN ('succeeded','failed')
                   OR OLD.authoring_execution_owner IS NULL OR OLD.authoring_execution_token_sha256 IS NULL
                   OR NEW.authoring_execution_owner IS NOT NULL OR NEW.authoring_execution_token_sha256 IS NOT NULL
                   OR NEW.authoring_execution_generation <> OLD.authoring_execution_generation THEN
                    RAISE EXCEPTION 'geometry inspection terminal publication requires its original execution claim' USING ERRCODE = '23514';
                END IF;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM artifact_versions AS v JOIN artifacts AS a
                  ON a.artifact_id = v.artifact_id AND a.owner_id = v.owner_id AND a.task_id = v.task_id
                WHERE v.owner_id = NEW.owner_id AND v.tenant_id = NEW.tenant_id AND v.task_id = NEW.task_id
                  AND v.artifact_id = NEW.inspection_request_artifact_id
                  AND v.version_id = NEW.inspection_request_artifact_version_id
                  AND v.sha256 = NEW.inspection_request_artifact_sha256
                  AND v.content_type = 'application/json' AND a.kind = 'geometry_inspection_request'
                  AND a.run_id = NEW.run_id AND a.status IN ('ready','available')
            ) THEN
                RAISE EXCEPTION 'geometry inspection exact request Artifact is invalid' USING ERRCODE = '23503';
            END IF;
            IF NEW.operation_detail_json ->> 'source_kind' = 'geometry_sample' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM geometry_resource_versions AS v JOIN geometry_resources AS r ON r.resource_id = v.resource_id
                    WHERE v.resource_id = NEW.operation_detail_json ->> 'source_id'
                      AND v.resource_version_id = NEW.inspection_source_geometry_version_id
                      AND v.content_digest = NEW.operation_detail_json ->> 'source_content_digest'
                      AND r.resource_kind = 'modeling_function'
                ) THEN RAISE EXCEPTION 'geometry inspection exact Geometry source is invalid' USING ERRCODE = '23503'; END IF;
            ELSE
                IF NOT EXISTS (
                    SELECT 1 FROM product_family_versions AS v
                    WHERE v.family_id = NEW.operation_detail_json ->> 'source_id'
                      AND v.family_version_id = NEW.inspection_source_family_version_id
                      AND v.content_digest = NEW.operation_detail_json ->> 'source_content_digest'
                ) THEN RAISE EXCEPTION 'geometry inspection exact Family source is invalid' USING ERRCODE = '23503'; END IF;
            END IF;
            IF NEW.status = 'succeeded' AND NOT EXISTS (
                SELECT 1 FROM artifact_versions AS v JOIN artifacts AS a
                  ON a.artifact_id = v.artifact_id AND a.owner_id = v.owner_id AND a.task_id = v.task_id
                WHERE v.owner_id = NEW.owner_id AND v.tenant_id = NEW.tenant_id AND v.task_id = NEW.task_id
                  AND v.artifact_id = NEW.inspection_result_artifact_id
                  AND v.version_id = NEW.inspection_result_artifact_version_id
                  AND v.sha256 = NEW.inspection_result_artifact_sha256
                  AND v.content_type = 'application/vnd.cad2bim.geometry-inspection+zip'
                  AND a.kind = 'geometry_inspection' AND a.run_id = NEW.run_id AND a.status IN ('ready','available')
                  AND v.metadata_json ->> 'format' = 'geometry_inspection_v1'
                  AND v.metadata_json ->> 'request_digest' = NEW.request_fingerprint
                  AND v.metadata_json ->> 'source_kind' = NEW.operation_detail_json ->> 'source_kind'
            ) THEN
                RAISE EXCEPTION 'geometry inspection exact result Artifact is invalid' USING ERRCODE = '23503';
            END IF;
            RETURN NEW;
        END
        $guard$
    """))
    op.execute(sa.text("CREATE TRIGGER trg_geometry_inspection_operation_guard BEFORE INSERT OR UPDATE "
        "ON critical_operations FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_geometry_inspection_operation()"))


def upgrade():
    for name in (*_REFERENCE_COLUMNS, *_SOURCE_COLUMNS):
        op.add_column(_TABLE, sa.Column(name, sa.String(64 if name.endswith("sha256") else 80), nullable=True))
    for role in ("request", "result"):
        op.create_foreign_key(f"fk_critical_operations_inspection_{role}_artifact", _TABLE, "artifact_versions",
            ("owner_id", "task_id", f"inspection_{role}_artifact_id", f"inspection_{role}_artifact_version_id"),
            ("owner_id", "task_id", "artifact_id", "version_id"), ondelete="RESTRICT")
    for role, table, column in (("geometry", "geometry_resource_versions", "resource_version_id"),
                               ("family", "product_family_versions", "family_version_id")):
        op.create_foreign_key(f"fk_critical_operations_inspection_{role}_source", _TABLE, table,
            (f"inspection_source_{role}_version_id",), (column,), ondelete="RESTRICT")
    op.create_unique_constraint("uq_critical_operations_inspection_result", _TABLE, ("inspection_result_artifact_version_id",))
    _checks(inspection=True)
    op.create_check_constraint(op.f(_PREFIX + "inspection_artifact_bindings_valid"), _TABLE, _artifact_bindings())
    op.create_check_constraint(op.f(_PREFIX + "inspection_detail_valid"), _TABLE, _DETAIL_SHAPE)
    _install_guard()
    task = "task_id = NULLIF(current_setting('cad2bim.task_id', true), '')"
    roles = "(pg_has_role(session_user,'cad2bim_mcp_runtime','MEMBER') OR pg_has_role(session_user,'cad2bim_runner_runtime','MEMBER'))"
    op.execute(sa.text("CREATE POLICY geometry_inspection_task_isolation ON critical_operations AS RESTRICTIVE "
        f"USING (operation_kind <> 'geometry_inspection' OR {task}) "
        f"WITH CHECK (operation_kind <> 'geometry_inspection' OR ({task} AND {roles}))"))
    op.execute(sa.text("CREATE POLICY geometry_inspection_asset_task_isolation ON artifacts AS RESTRICTIVE "
        f"USING (kind NOT IN ('geometry_inspection','geometry_inspection_request') OR {task}) "
        f"WITH CHECK (kind NOT IN ('geometry_inspection','geometry_inspection_request') OR ({task} AND {roles}))"))
    for role in ("cad2bim_mcp_runtime", "cad2bim_runner_runtime"):
        op.execute(sa.text("GRANT UPDATE (inspection_result_artifact_id, inspection_result_artifact_version_id, "
            f"inspection_result_artifact_sha256) ON TABLE critical_operations TO {role}"))


def downgrade():
    op.execute(sa.text("LOCK TABLE critical_operations, opaque_reference_index IN ACCESS EXCLUSIVE MODE"))
    op.execute(sa.text("""DO $guard$ BEGIN
        IF EXISTS (SELECT 1 FROM critical_operations WHERE operation_kind = 'geometry_inspection')
           OR EXISTS (SELECT 1 FROM opaque_reference_index WHERE reference_kind = 'gi') THEN
            RAISE EXCEPTION 'Cannot remove retained geometry inspection Operations or references' USING ERRCODE = '23514';
        END IF;
    END $guard$"""))
    op.execute(sa.text("DROP POLICY geometry_inspection_task_isolation ON critical_operations"))
    op.execute(sa.text("DROP POLICY geometry_inspection_asset_task_isolation ON artifacts"))
    op.execute(sa.text("DROP TRIGGER trg_geometry_inspection_operation_guard ON critical_operations"))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_geometry_inspection_operation()"))
    for role in ("cad2bim_mcp_runtime", "cad2bim_runner_runtime"):
        op.execute(sa.text("REVOKE UPDATE (inspection_result_artifact_id, inspection_result_artifact_version_id, "
            f"inspection_result_artifact_sha256) ON TABLE critical_operations FROM {role}"))
    _checks(inspection=False)
    for name in ("inspection_detail_valid", "inspection_artifact_bindings_valid"):
        op.drop_constraint(op.f(_PREFIX + name), _TABLE, type_="check")
    op.drop_constraint("uq_critical_operations_inspection_result", _TABLE, type_="unique")
    for role in ("request", "result"):
        op.drop_constraint(f"fk_critical_operations_inspection_{role}_artifact", _TABLE, type_="foreignkey")
    for role in ("geometry", "family"):
        op.drop_constraint(f"fk_critical_operations_inspection_{role}_source", _TABLE, type_="foreignkey")
    for name in reversed((*_REFERENCE_COLUMNS, *_SOURCE_COLUMNS)):
        op.drop_column(_TABLE, name)
