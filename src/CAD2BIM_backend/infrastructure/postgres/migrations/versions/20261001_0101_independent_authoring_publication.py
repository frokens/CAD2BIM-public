"""Admit independent publication execution on the original critical Operation.

Revision ID: 20261001_0101
Revises: 20260930_0100
"""
from importlib import import_module

from alembic import op
import sqlalchemy as sa

revision = "20261001_0101"
down_revision = "20260930_0100"
branch_labels = None
depends_on = None


def _previous(name):
    return import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions." + name)


_PUBLISH_DETAIL = """
 operation_kind='authoring_publish' AND tool_name='ifc_authoring_publish'
 AND operation_detail_json->>'detail_kind'='authoring_publish'
 AND operation_detail_json - ARRAY['detail_kind','root_kind','root_id','expected_drafts','preview_examples','geometry_versions','family_version']='{}'::jsonb
 AND jsonb_typeof(operation_detail_json->'expected_drafts')='array'
 AND jsonb_array_length(operation_detail_json->'expected_drafts')>0
 AND jsonb_typeof(operation_detail_json->'preview_examples')='array'
 AND jsonb_typeof(operation_detail_json->'geometry_versions')='array'
 AND result_package_id IS NULL AND source_filename IS NULL AND description_artifact_id IS NULL
 AND code_artifact_id IS NULL AND repair_parent_operation_id IS NULL
 AND base_ifc_revision_id IS NULL AND expected_ifc_write_epoch IS NULL
 AND ((operation_detail_json->>'root_kind'='geometry'
   AND target_geometry_resource_id=operation_detail_json->>'root_id' AND target_product_family_id IS NULL
   AND result_product_family_version_id IS NULL AND base_product_family_version_id IS NULL)
  OR (operation_detail_json->>'root_kind'='family'
   AND target_product_family_id=operation_detail_json->>'root_id' AND target_geometry_resource_id IS NULL
   AND result_geometry_resource_version_id IS NULL))
 AND ((status IN ('prepared','failed') AND operation_detail_json->'geometry_versions'='[]'::jsonb
       AND operation_detail_json->'family_version'='null'::jsonb AND base_product_family_version_id IS NULL)
   OR (status='succeeded' AND ((operation_detail_json->>'root_kind'='geometry'
       AND result_geometry_resource_version_id IS NOT NULL
       AND operation_detail_json->'geometry_versions' @> jsonb_build_array(jsonb_build_object(
         'resource_id',target_geometry_resource_id,'resource_version_id',result_geometry_resource_version_id)))
     OR (operation_detail_json->>'root_kind'='family'
       AND result_product_family_version_id=operation_detail_json#>>'{family_version,family_version_id}'))))
"""


def _checks():
    old = _previous("20260930_0100_authoring_drafts")
    detail = _previous("20260929_0097_system_family_publication")._detail_check(system_copy=True)
    expression = f"({detail}) OR operation_kind='geometry_inspection' OR COALESCE(({old._DRAFT_DETAIL}),FALSE) OR COALESCE(({_PUBLISH_DETAIL}),FALSE)"
    op.drop_constraint(op.f("ck_critical_operations_operation_kind_detail_valid"), "critical_operations", type_="check")
    # Preserve immutable old receipts without granting them current input/read eligibility.
    # NOT VALID still enforces the complete current contract for every INSERT/UPDATE.
    op.execute(sa.text("ALTER TABLE critical_operations ADD CONSTRAINT ck_critical_operations_operation_kind_detail_valid CHECK (" + expression + ") NOT VALID"))
    kinds = "'geometry_resource_write','product_family_write','geometry_inspection','authoring_publish'"
    old._replace_check("authoring_control_consistent",
        f"(operation_kind NOT IN ({kinds}) AND authoring_execution_owner IS NULL "
        "AND authoring_execution_generation=0 AND authoring_execution_token_sha256 IS NULL) OR "
        f"(operation_kind IN ({kinds}) AND authoring_execution_generation>=0 AND "
        "((authoring_execution_owner IS NULL AND authoring_execution_token_sha256 IS NULL) OR "
        "(authoring_execution_owner IS NOT NULL AND authoring_execution_token_sha256 IS NOT NULL "
        "AND length(authoring_execution_token_sha256)=64 AND authoring_execution_generation>0)))")
    phases = _previous("20260904_0074_product_family_authoring")._error_phase_check(product_family=True)
    old._replace_check("error_phase_valid", phases + " OR (status='failed' "
        "AND operation_kind IN ('geometry_inspection','authoring_publish') AND error_phase IN "
        "('code_validation','execution','output_validation','storage_verification','commit_fence'))")
    old._replace_check("reference_kind_valid", "reference_kind IN ('gr','gv','fr','fv','sc','gp','pp','gq','gi','gd','fd','or')", "opaque_reference_index")


def _guard():
    _previous("20260920_0087_ephemeral_authoring_validation")._replace_function(
        "cad2bim_guard_authoring_short_operation",
        "IF NEW.operation_kind<>'authoring_publish' AND NEW.operation_detail_json->>'detail_kind' IS DISTINCT FROM 'authoring_draft_write' THEN",
        "IF NEW.operation_detail_json->>'detail_kind' IS DISTINCT FROM 'authoring_draft_write' THEN",
    )
    op.execute(sa.text("""
    CREATE FUNCTION cad2bim_guard_authoring_publication_operation() RETURNS trigger LANGUAGE plpgsql AS $guard$
    DECLARE item jsonb; generation bigint; snapshot_id text; current_scope text; tenant text; task text;
    BEGIN
      IF TG_OP='UPDATE' AND OLD.operation_kind IS DISTINCT FROM NEW.operation_kind THEN
        RAISE EXCEPTION 'critical Operation kind is immutable' USING ERRCODE='23514'; END IF;
      IF NEW.operation_kind<>'authoring_publish' THEN RETURN NEW; END IF;
      IF TG_OP='INSERT' THEN
        IF NEW.status<>'prepared' OR NEW.authoring_execution_owner IS NOT NULL
           OR NEW.authoring_execution_generation<>0 THEN
          RAISE EXCEPTION 'publication begins with unclaimed admission' USING ERRCODE='23514'; END IF;
      ELSE
        IF OLD.status<>'prepared' THEN
          RAISE EXCEPTION 'publication terminal record is immutable' USING ERRCODE='23514'; END IF;
        IF to_jsonb(NEW)-ARRAY['status','operation_detail_json','result_geometry_resource_version_id',
            'result_product_family_version_id','base_product_family_version_id','error_code','error_message',
            'error_phase','finished_at','authoring_execution_owner','authoring_execution_generation','authoring_execution_token_sha256']
          IS DISTINCT FROM to_jsonb(OLD)-ARRAY['status','operation_detail_json','result_geometry_resource_version_id',
            'result_product_family_version_id','base_product_family_version_id','error_code','error_message',
            'error_phase','finished_at','authoring_execution_owner','authoring_execution_generation','authoring_execution_token_sha256']
          OR NEW.operation_detail_json-ARRAY['geometry_versions','family_version']
             IS DISTINCT FROM OLD.operation_detail_json-ARRAY['geometry_versions','family_version'] THEN
          RAISE EXCEPTION 'publication admitted request is immutable' USING ERRCODE='23514'; END IF;
        IF NEW.status='prepared' THEN
          IF NEW.authoring_execution_owner IS NULL OR NEW.authoring_execution_token_sha256 IS NULL
             OR NEW.authoring_execution_generation<>OLD.authoring_execution_generation+1
             OR NEW.operation_detail_json IS DISTINCT FROM OLD.operation_detail_json THEN
            RAISE EXCEPTION 'publication claim generation is invalid' USING ERRCODE='23514'; END IF;
          RETURN NEW;
        ELSIF NEW.status NOT IN ('succeeded','failed') OR OLD.authoring_execution_owner IS NULL
          OR NEW.authoring_execution_generation<>OLD.authoring_execution_generation
          OR NEW.authoring_execution_owner IS NOT NULL OR NEW.authoring_execution_token_sha256 IS NOT NULL THEN
          RAISE EXCEPTION 'publication completion requires its claimed generation' USING ERRCODE='23514'; END IF;
      END IF;
      IF NEW.status='failed' THEN RETURN NEW; END IF;
      FOR item IN SELECT value FROM jsonb_array_elements(NEW.operation_detail_json->'expected_drafts') LOOP
        IF item->>'root_kind'='geometry' THEN
          SELECT r.draft_generation,r.draft_artifact_version_id,r.owner_scope,r.owner_tenant_id,r.owner_task_id
            INTO generation,snapshot_id,current_scope,tenant,task FROM geometry_resources r WHERE r.resource_id=item->>'root_id' FOR UPDATE;
        ELSIF item->>'root_kind'='family' THEN
          SELECT r.draft_generation,r.draft_artifact_version_id,r.owner_scope,r.owner_tenant_id,r.owner_task_id
            INTO generation,snapshot_id,current_scope,tenant,task FROM product_families r WHERE r.family_id=item->>'root_id' FOR UPDATE;
        ELSE RAISE EXCEPTION 'unknown publication draft kind' USING ERRCODE='23514'; END IF;
        IF NOT FOUND OR generation IS DISTINCT FROM (item->>'generation')::bigint
          OR snapshot_id IS DISTINCT FROM item#>>'{snapshot,version_id}' THEN
          RAISE EXCEPTION 'publication draft changed after admission' USING ERRCODE='23514'; END IF;
        IF NOT ((current_scope='TASK' AND tenant=NEW.tenant_id AND task=NEW.task_id)
          OR (current_scope='SYSTEM' AND pg_has_role(session_user,'cad2bim_system_curator','MEMBER'))) THEN
          RAISE EXCEPTION 'publication requires draft ownership' USING ERRCODE='42501'; END IF;
        IF NOT EXISTS (SELECT 1 FROM artifact_versions v WHERE v.owner_id=NEW.owner_id AND v.tenant_id=NEW.tenant_id
          AND v.task_id=NEW.task_id AND v.version_id=snapshot_id AND v.artifact_id=item#>>'{snapshot,artifact_id}'
          AND v.sha256=item#>>'{snapshot,sha256}') THEN
          RAISE EXCEPTION 'publication snapshot is not exact' USING ERRCODE='23503'; END IF;
      END LOOP;
      IF NEW.status='succeeded' THEN
        FOR item IN SELECT value FROM jsonb_array_elements(NEW.operation_detail_json->'geometry_versions') LOOP
          IF NOT EXISTS (SELECT 1 FROM geometry_resource_versions v WHERE v.resource_id=item->>'resource_id'
            AND v.resource_version_id=item->>'resource_version_id') THEN
            RAISE EXCEPTION 'publication Geometry result is missing' USING ERRCODE='23503'; END IF;
        END LOOP;
      END IF;
      RETURN NEW;
    END $guard$;
    CREATE TRIGGER trg_authoring_publication_operation_guard BEFORE INSERT OR UPDATE ON critical_operations
      FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_authoring_publication_operation();
    """))


def upgrade():
    _checks()
    _guard()
    _family_standard_query()
    for role in ("cad2bim_mcp_runtime", "cad2bim_runner_runtime", "cad2bim_system_curator"):
        op.execute(sa.text("GRANT UPDATE (operation_detail_json,base_product_family_version_id,"
                           f"result_geometry_resource_version_id) ON critical_operations TO {role}"))


def _family_standard_query():
    table = "geometry_query_attempts"
    op.add_column(table, sa.Column("family_version_id", sa.String(80), nullable=True))
    op.create_foreign_key(op.f("fk_geometry_query_attempts_family_version"), table, "product_family_versions",
                         ["family_version_id"], ["family_version_id"], ondelete="RESTRICT")
    prior = _previous("20260929_0098_geometry_inspection_query")._TARGET_SHAPE
    _previous("20260930_0100_authoring_drafts")._replace_check("target_shape_valid",
        f"(family_version_id IS NULL AND ({prior})) OR "
        "(target_kind='family_standard_model' AND family_version_id IS NOT NULL "
        "AND resource_version_id IS NULL AND target_artifact_version_id IS NULL "
        "AND target_geometry_format IS NULL AND inspection_selection_json IS NULL "
        "AND inspection_coordinate_space_json IS NULL)", table)
    patch = _previous("20260920_0087_ephemeral_authoring_validation")._replace_function
    patch("cad2bim_guard_geometry_query_attempt", "ELSIF NEW.target_kind = 'geometry_inspection' THEN", """
      ELSIF NEW.target_kind = 'family_standard_model' THEN
        IF NOT EXISTS (SELECT 1 FROM product_family_versions v
          WHERE v.family_version_id=NEW.family_version_id AND v.abi_profile='product_realization_v4'
          AND v.family_contract_json->>'schema'='product_family_contract_v4') THEN
          RAISE EXCEPTION 'Family standard model query target is unavailable' USING ERRCODE='23503'; END IF;
      ELSIF NEW.target_kind = 'geometry_inspection' THEN""")
    patch("cad2bim_guard_geometry_query_attempt", "NEW.resource_version_id,",
          "NEW.resource_version_id, NEW.family_version_id,")
    patch("cad2bim_guard_geometry_query_attempt", "OLD.resource_version_id,",
          "OLD.resource_version_id, OLD.family_version_id,")
    patch("cad2bim_guard_geometry_query_attempt", "AND parent.target_kind = NEW.target_kind",
          "AND parent.target_kind = NEW.target_kind AND parent.family_version_id IS NOT DISTINCT FROM NEW.family_version_id")


def downgrade():
    raise RuntimeError("publication execution migration is irreversible; restore the pre-cutover database backup")
