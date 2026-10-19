"""Editable root drafts and atomic publication from retained Preview evidence.

Revision ID: 20260930_0100
Revises: 20260929_0099
"""

from importlib import import_module

from alembic import op
import sqlalchemy as sa

revision = "20260930_0100"
down_revision = "20260929_0099"
branch_labels = None
depends_on = None

_DRAFT_DETAIL = """
 operation_kind IN ('geometry_resource_write','product_family_write')
 AND operation_detail_json ->> 'detail_kind' = 'authoring_draft_write'
 AND operation_detail_json - ARRAY['detail_kind','root_kind','root_id','expected_generation','result_generation'] = '{}'::jsonb
 AND code_artifact_id IS NOT NULL AND code_artifact_version_id IS NOT NULL AND code_artifact_sha256 IS NOT NULL
 AND repair_parent_operation_id IS NULL AND base_product_family_version_id IS NULL
 AND result_geometry_resource_version_id IS NULL AND result_product_family_version_id IS NULL
 AND result_package_id IS NULL AND source_filename IS NULL AND description_artifact_id IS NULL
 AND base_ifc_revision_id IS NULL AND expected_ifc_write_epoch IS NULL
 AND (operation_detail_json ->> 'result_generation')::bigint > 0
 AND (operation_detail_json ->> 'result_generation')::bigint = COALESCE((operation_detail_json ->> 'expected_generation')::bigint,0)+1
 AND ((operation_kind='geometry_resource_write' AND tool_name='ifc_geometry_resource_write'
   AND operation_detail_json ->> 'root_kind'='geometry' AND target_product_family_id IS NULL
   AND target_geometry_resource_id=operation_detail_json ->> 'root_id')
  OR (operation_kind='product_family_write' AND tool_name='ifc_product_family_write'
   AND operation_detail_json ->> 'root_kind'='family' AND target_geometry_resource_id IS NULL
   AND target_product_family_id=operation_detail_json ->> 'root_id'))
"""
_PUBLISH_DETAIL = """
 operation_kind='authoring_publish' AND tool_name='ifc_authoring_publish'
 AND operation_detail_json ->> 'detail_kind'='authoring_publish'
 AND operation_detail_json - ARRAY['detail_kind','root_kind','root_id','expected_drafts','evidence_run_id','evidence','geometry_versions','family_version'] = '{}'::jsonb
 AND jsonb_typeof(operation_detail_json -> 'evidence_run_id')='string'
 AND length(operation_detail_json ->> 'evidence_run_id') BETWEEN 1 AND 80
 AND jsonb_typeof(operation_detail_json -> 'expected_drafts')='array'
 AND jsonb_array_length(operation_detail_json -> 'expected_drafts')>0
 AND jsonb_typeof(operation_detail_json -> 'evidence')='array'
 AND jsonb_array_length(operation_detail_json -> 'evidence')>0
 AND jsonb_typeof(operation_detail_json -> 'geometry_versions')='array'
 AND result_package_id IS NULL AND source_filename IS NULL AND description_artifact_id IS NULL
 AND repair_parent_operation_id IS NULL AND base_ifc_revision_id IS NULL AND expected_ifc_write_epoch IS NULL
 AND ((operation_detail_json ->> 'root_kind'='geometry'
   AND target_geometry_resource_id=operation_detail_json ->> 'root_id'
   AND result_geometry_resource_version_id IS NOT NULL AND target_product_family_id IS NULL
   AND result_product_family_version_id IS NULL AND base_product_family_version_id IS NULL)
  OR (operation_detail_json ->> 'root_kind'='family'
   AND target_product_family_id=operation_detail_json ->> 'root_id'
   AND result_product_family_version_id=operation_detail_json #>> '{family_version,family_version_id}'
   AND target_geometry_resource_id IS NULL AND result_geometry_resource_version_id IS NULL))
"""
_SHORT_TERMINAL = """
 status='succeeded' AND finished_at IS NOT NULL AND error_code IS NULL AND error_message IS NULL AND error_phase IS NULL
 AND (operation_kind='authoring_publish' OR operation_detail_json ->> 'detail_kind'='authoring_draft_write')
"""


def _previous(name):
    return import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions." + name)


def _replace_check(name, expression, table="critical_operations"):
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, expression)


def _root_drafts():
    for table in ("geometry_resources", "product_families"):
        op.add_column(table, sa.Column("draft_artifact_version_id", sa.String(80), nullable=True))
        op.add_column(table, sa.Column("draft_generation", sa.BigInteger(), nullable=False, server_default="0"))
        op.create_foreign_key(op.f(f"fk_{table}_draft_artifact_version_id_artifact_versions"), table, "artifact_versions",
                             ["draft_artifact_version_id"], ["version_id"], ondelete="RESTRICT")
        op.create_check_constraint(op.f(f"ck_{table}_draft_binding_consistent"), table,
            "(draft_generation=0 AND draft_artifact_version_id IS NULL) OR "
            "(draft_generation>0 AND draft_artifact_version_id IS NOT NULL)")
        for role in ("cad2bim_mcp_runtime", "cad2bim_runner_runtime", "cad2bim_system_curator"):
            op.execute(sa.text(f"GRANT UPDATE (draft_artifact_version_id,draft_generation) ON {table} TO {role}"))
        owned = "owner_scope='TASK' AND owner_tenant_id=NULLIF(current_setting('cad2bim.tenant_id',true),'') " \
                "AND owner_task_id=NULLIF(current_setting('cad2bim.task_id',true),'')"
        op.execute(sa.text(f"CREATE POLICY {table}_draft_task_update ON {table} FOR UPDATE "
            f"TO cad2bim_mcp_runtime,cad2bim_runner_runtime USING ({owned}) WITH CHECK ({owned})"))
        op.execute(sa.text(f"CREATE POLICY {table}_draft_system_update ON {table} FOR UPDATE "
            "TO cad2bim_system_curator USING (owner_scope='SYSTEM') WITH CHECK (owner_scope='SYSTEM')"))
    op.execute(sa.text("""
      CREATE FUNCTION cad2bim_guard_authoring_draft_root() RETURNS trigger LANGUAGE plpgsql AS $guard$
      DECLARE snapshot artifact_versions%ROWTYPE; root_id text;
      BEGIN
        IF TG_OP='UPDATE' AND (to_jsonb(NEW)-ARRAY['draft_artifact_version_id','draft_generation'])
            IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['draft_artifact_version_id','draft_generation']) THEN
          RAISE EXCEPTION 'resource root identity is immutable' USING ERRCODE='23514';
        END IF;
        IF NEW.draft_artifact_version_id IS NULL THEN
          IF TG_OP='UPDATE' THEN RAISE EXCEPTION 'draft cannot be cleared' USING ERRCODE='23514'; END IF;
          RETURN NEW;
        END IF;
        IF NEW.draft_generation <> (CASE WHEN TG_OP='INSERT' THEN 1 ELSE OLD.draft_generation+1 END) THEN
          RAISE EXCEPTION 'draft generation must advance exactly once' USING ERRCODE='23514';
        END IF;
        IF NOT ((NEW.owner_scope='TASK' AND NEW.owner_tenant_id=NULLIF(current_setting('cad2bim.tenant_id',true),'')
          AND NEW.owner_task_id=NULLIF(current_setting('cad2bim.task_id',true),''))
          OR (NEW.owner_scope='SYSTEM' AND pg_has_role(session_user,'cad2bim_system_curator','MEMBER'))) THEN
          RAISE EXCEPTION 'draft mutation requires resource owner' USING ERRCODE='42501';
        END IF;
        SELECT v.* INTO snapshot FROM artifact_versions v JOIN artifacts a ON a.artifact_id=v.artifact_id
          WHERE v.version_id=NEW.draft_artifact_version_id
          AND v.tenant_id=NULLIF(current_setting('cad2bim.tenant_id',true),'')
          AND v.task_id=NULLIF(current_setting('cad2bim.task_id',true),'')
          AND v.content_type='application/vnd.cad2bim.authoring-draft+json'
          AND a.kind='authoring_draft' AND a.status IN ('ready','available');
        IF NOT FOUND THEN RAISE EXCEPTION 'draft snapshot is outside exact Task scope' USING ERRCODE='23503'; END IF;
        RETURN NEW;
      END $guard$;
      CREATE FUNCTION cad2bim_check_authoring_draft_commit() RETURNS trigger LANGUAGE plpgsql AS $guard$
      DECLARE root_id text; root_kind text;
      BEGIN
        IF NEW.draft_artifact_version_id IS NULL THEN RETURN NEW; END IF;
        root_kind := CASE WHEN TG_TABLE_NAME='geometry_resources' THEN 'geometry' ELSE 'family' END;
        root_id := CASE WHEN root_kind='geometry' THEN to_jsonb(NEW)->>'resource_id' ELSE to_jsonb(NEW)->>'family_id' END;
        IF NOT EXISTS (SELECT 1 FROM critical_operations o
          WHERE o.operation_detail_json->>'detail_kind'='authoring_draft_write' AND o.status='succeeded'
          AND o.operation_detail_json->>'root_kind'=root_kind AND o.operation_detail_json->>'root_id'=root_id
          AND (o.operation_detail_json->>'result_generation')::bigint=NEW.draft_generation
          AND o.code_artifact_version_id=NEW.draft_artifact_version_id) THEN
          RAISE EXCEPTION 'draft mutation requires its atomic successful Operation' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END $guard$;
    """))
    for table in ("geometry_resources", "product_families"):
        op.execute(sa.text(f"CREATE TRIGGER trg_{table}_draft_guard BEFORE INSERT OR UPDATE ON {table} "
                          "FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_authoring_draft_root()"))
        op.execute(sa.text(f"CREATE CONSTRAINT TRIGGER trg_{table}_draft_commit AFTER INSERT OR UPDATE ON {table} "
                          "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cad2bim_check_authoring_draft_commit()"))


def _short_operations():
    op.drop_constraint("uq_critical_operations_result_product_family_version", "critical_operations", type_="unique")
    op.drop_index("uq_critical_operations_run_tool_fingerprint", table_name="critical_operations")
    op.create_index("uq_critical_operations_run_tool_fingerprint", "critical_operations",
        ["run_id", "tool_name", "request_fingerprint"], unique=True,
        postgresql_where=sa.text("operation_kind IN ('source_ingestion','data_transform',"
                                "'geometry_resource_write','product_family_write','authoring_publish')"))
    op.execute(sa.text("DROP POLICY product_family_authoring_task_isolation ON critical_operations"))
    op.execute(sa.text("""
      CREATE POLICY product_family_authoring_task_isolation ON critical_operations AS RESTRICTIVE
      USING (operation_kind<>'product_family_write' OR task_id=NULLIF(current_setting('cad2bim.task_id',true),''))
      WITH CHECK (operation_kind<>'product_family_write' OR (
        task_id=NULLIF(current_setting('cad2bim.task_id',true),'') AND (
          pg_has_role(session_user,'cad2bim_mcp_runtime','MEMBER')
          OR pg_has_role(session_user,'cad2bim_runner_runtime','MEMBER')
          OR (pg_has_role(session_user,'cad2bim_system_curator','MEMBER') AND (
            operation_detail_json->>'mode'='system_copy'
            OR operation_detail_json->>'detail_kind'='authoring_draft_write')))))
    """))
    # Keep historical immutable rows. Only current admissions may create new rows.
    for function in ("cad2bim_guard_geometry_authoring_operation", "cad2bim_guard_product_family_authoring_operation",
                     "cad2bim_guard_product_family_current_operation"):
        op.execute(sa.text(f"""
          DO $patch$ DECLARE definition text; BEGIN
            SELECT pg_get_functiondef('{function}()'::regprocedure) INTO definition;
            EXECUTE regexp_replace(definition, 'BEGIN', 'BEGIN
              IF NEW.operation_detail_json->>''detail_kind''=''authoring_draft_write'' THEN RETURN NEW; END IF;', '');
          END $patch$;
        """))
    op.execute(sa.text("""
      CREATE FUNCTION cad2bim_guard_authoring_short_operation() RETURNS trigger LANGUAGE plpgsql AS $guard$
      DECLARE item jsonb; generation bigint; snapshot_id text; current_scope text; tenant text; task text;
      BEGIN
        IF NEW.operation_kind='geometry_resource_write' AND NEW.operation_detail_json->>'detail_kind' IS DISTINCT FROM 'authoring_draft_write'
          OR NEW.operation_kind='product_family_write' AND NEW.operation_detail_json->>'detail_kind' IS DISTINCT FROM 'authoring_draft_write'
             AND NEW.operation_detail_json->>'mode' IS DISTINCT FROM 'system_copy' THEN
          RAISE EXCEPTION 'old executing authoring Write is retired' USING ERRCODE='23514';
        END IF;
        IF NEW.operation_kind<>'authoring_publish' AND NEW.operation_detail_json->>'detail_kind' IS DISTINCT FROM 'authoring_draft_write' THEN
          RETURN NEW;
        END IF;
        IF TG_OP<>'INSERT' OR NEW.status<>'succeeded' OR NEW.authoring_execution_owner IS NOT NULL
          OR NEW.authoring_execution_generation<>0 OR NEW.authoring_execution_token_sha256 IS NOT NULL THEN
          RAISE EXCEPTION 'short authoring Operations are immutable terminal inserts' USING ERRCODE='23514';
        END IF;
        IF NEW.operation_detail_json->>'detail_kind'='authoring_draft_write' THEN
          IF NOT EXISTS (SELECT 1 FROM artifact_versions v JOIN artifacts a ON a.artifact_id=v.artifact_id
            WHERE v.owner_id=NEW.owner_id AND v.tenant_id=NEW.tenant_id AND v.task_id=NEW.task_id
            AND v.artifact_id=NEW.code_artifact_id AND v.version_id=NEW.code_artifact_version_id
            AND v.sha256=NEW.code_artifact_sha256 AND v.content_type='application/vnd.cad2bim.authoring-draft+json'
            AND a.kind='authoring_draft'
            AND a.status IN ('ready','available')) THEN
            RAISE EXCEPTION 'draft Operation snapshot is not exact' USING ERRCODE='23503';
          END IF;
          RETURN NEW;
        END IF;
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
            RAISE EXCEPTION 'publication draft changed after Preview' USING ERRCODE='23514';
          END IF;
          IF NOT ((current_scope='TASK' AND tenant=NEW.tenant_id AND task=NEW.task_id)
            OR (current_scope='SYSTEM' AND pg_has_role(session_user,'cad2bim_system_curator','MEMBER'))) THEN
            RAISE EXCEPTION 'publication requires draft ownership' USING ERRCODE='42501';
          END IF;
          IF NOT EXISTS (SELECT 1 FROM artifact_versions v WHERE v.owner_id=NEW.owner_id AND v.tenant_id=NEW.tenant_id
            AND v.task_id=NEW.task_id AND v.version_id=snapshot_id AND v.artifact_id=item#>>'{snapshot,artifact_id}'
            AND v.sha256=item#>>'{snapshot,sha256}') THEN
            RAISE EXCEPTION 'publication snapshot is not exact' USING ERRCODE='23503'; END IF;
        END LOOP;
        FOR item IN SELECT value FROM jsonb_array_elements(NEW.operation_detail_json->'geometry_versions') LOOP
          IF NOT EXISTS (SELECT 1 FROM geometry_resource_versions v WHERE v.resource_id=item->>'resource_id'
            AND v.resource_version_id=item->>'resource_version_id') THEN
            RAISE EXCEPTION 'publication Geometry result is missing' USING ERRCODE='23503'; END IF;
        END LOOP;
        RETURN NEW;
      END $guard$;
      CREATE TRIGGER trg_authoring_short_operation_guard BEFORE INSERT OR UPDATE ON critical_operations
        FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_authoring_short_operation();
    """))


def _operation_checks():
    old = _previous("20260929_0099_geometry_inspection_operation")
    family = _previous("20260904_0074_product_family_authoring")
    detail = _previous("20260929_0097_system_family_publication")._detail_check(system_copy=True)
    _replace_check("operation_kind_valid", "operation_kind IN ('source_ingestion','data_transform','ifc_model_write',"
                   "'geometry_resource_write','product_family_write','geometry_inspection','authoring_publish')")
    _replace_check("operation_kind_detail_valid", f"({detail}) OR operation_kind='geometry_inspection' OR "
                   f"COALESCE(({_DRAFT_DETAIL}),FALSE) OR COALESCE(({_PUBLISH_DETAIL}),FALSE)")
    terminal = family._terminal_check(product_family=True)
    _replace_check("terminal_result_valid", f"({terminal}) OR ({_SHORT_TERMINAL}) OR "
        "(operation_kind='geometry_inspection' AND status='succeeded' AND finished_at IS NOT NULL "
        "AND error_code IS NULL AND error_message IS NULL AND error_phase IS NULL AND result_package_id IS NULL "
        "AND result_geometry_resource_version_id IS NULL AND result_product_family_version_id IS NULL "
        "AND inspection_result_artifact_version_id IS NOT NULL)")
    bindings = old._artifact_bindings().replace("status = 'succeeded' AND", "(status = 'succeeded' OR "
        "(status = 'failed' AND operation_detail_json->>'result_kind'='case_report')) AND")
    _replace_check("inspection_artifact_bindings_valid", bindings)
    shape = old._DETAIL_SHAPE.replace("'request_artifact']", "'request_artifact','result_kind','source_type','source_draft_artifact']")
    shape = shape.replace("AND ((operation_detail_json ->> 'source_kind'", "AND ((operation_detail_json->>'source_type'='draft' "
        "AND inspection_source_geometry_version_id IS NULL AND inspection_source_family_version_id IS NULL "
        "AND operation_detail_json->>'source_version_id'=operation_detail_json#>>'{source_draft_artifact,version_id}') "
        "OR (operation_detail_json ->> 'source_kind'", 1)
    _replace_check("inspection_detail_valid", shape)


def _inspection_guard():
    # Preserve the existing claim/request/result immutability guard and change only
    # the source and result alternatives which gained explicit business meaning.
    old_source = "IF NEW.operation_detail_json ->> 'source_kind' = 'geometry_sample' THEN"
    new_source = """IF NEW.operation_detail_json->>'source_type'='draft' THEN
      IF NOT EXISTS (SELECT 1 FROM artifact_versions v WHERE v.owner_id=NEW.owner_id AND v.tenant_id=NEW.tenant_id
        AND v.task_id=NEW.task_id AND v.artifact_id=NEW.operation_detail_json#>>'{source_draft_artifact,artifact_id}'
        AND v.version_id=NEW.operation_detail_json#>>'{source_draft_artifact,version_id}'
        AND v.sha256=NEW.operation_detail_json#>>'{source_draft_artifact,sha256}') THEN
        RAISE EXCEPTION 'Preview draft source is not an exact private snapshot' USING ERRCODE='23503'; END IF;
      ELSIF NEW.operation_detail_json ->> 'source_kind' = 'geometry_sample' THEN"""
    old_result = "IF NEW.status = 'succeeded' AND NOT EXISTS ("
    new_result = "IF NEW.inspection_result_artifact_version_id IS NOT NULL AND NOT EXISTS ("
    old_type = "AND v.content_type = 'application/vnd.cad2bim.geometry-inspection+zip'"
    new_type = "AND v.content_type = CASE WHEN NEW.operation_detail_json->>'result_kind'='case_report' THEN 'application/json' ELSE 'application/vnd.cad2bim.geometry-inspection+zip' END"
    replacements = ((old_source, new_source), (old_result, new_result), (old_type, new_type),
        ("AND r.resource_kind = 'modeling_function'", ""),
        ("a.kind = 'geometry_inspection'", "a.kind = CASE WHEN NEW.operation_detail_json->>'result_kind'='case_report' THEN 'geometry_preview_case_report' ELSE 'geometry_inspection' END"),
        ("v.metadata_json ->> 'format' = 'geometry_inspection_v1'", "v.metadata_json ->> 'format' = CASE WHEN NEW.operation_detail_json->>'result_kind'='case_report' THEN 'geometry_preview_case_report_v1' ELSE 'geometry_inspection_v1' END"))
    for before, after in replacements:
        _previous("20260920_0087_ephemeral_authoring_validation")._replace_function(
            "cad2bim_guard_geometry_inspection_operation", before, after)


def _private_artifacts():
    task = "task_id=NULLIF(current_setting('cad2bim.task_id',true),'')"
    roles = "(pg_has_role(session_user,'cad2bim_mcp_runtime','MEMBER') OR " \
            "pg_has_role(session_user,'cad2bim_runner_runtime','MEMBER') OR " \
            "pg_has_role(session_user,'cad2bim_system_curator','MEMBER'))"
    op.execute(sa.text("CREATE POLICY authoring_publish_task_isolation ON critical_operations AS RESTRICTIVE "
        f"USING (operation_kind<>'authoring_publish' OR {task}) "
        f"WITH CHECK (operation_kind<>'authoring_publish' OR ({task} AND {roles}))"))
    op.execute(sa.text("DROP POLICY geometry_inspection_task_isolation ON critical_operations"))
    op.execute(sa.text("CREATE POLICY geometry_inspection_task_isolation ON critical_operations AS RESTRICTIVE "
        f"USING (operation_kind<>'geometry_inspection' OR {task}) "
        f"WITH CHECK (operation_kind<>'geometry_inspection' OR ({task} AND {roles}))"))
    op.execute(sa.text("DROP POLICY geometry_inspection_asset_task_isolation ON artifacts"))
    kinds = "('geometry_inspection','geometry_inspection_request','geometry_preview_case_report','authoring_draft')"
    op.execute(sa.text("CREATE POLICY geometry_inspection_asset_task_isolation ON artifacts AS RESTRICTIVE "
        f"USING (kind NOT IN {kinds} OR {task}) "
        f"WITH CHECK (kind NOT IN {kinds} OR ({task} AND {roles}))"))
    # Extend the existing security-definer lookup rather than introducing an
    # alternate registry; a public SYSTEM root must not grant Artifact access.
    op.execute(sa.text("""
      DO $patch$ DECLARE definition text; BEGIN
        SELECT pg_get_functiondef('cad2bim_geometry_source_artifact_visible(text,text,text)'::regprocedure)
          INTO definition;
        IF position($before$artifact.kind <> 'geometry_authoring_source'$before$ IN definition)=0 THEN
          RAISE EXCEPTION 'Expected source visibility guard is absent' USING ERRCODE='23514'; END IF;
        EXECUTE replace(definition,$before$artifact.kind <> 'geometry_authoring_source'$before$,
          $after$artifact.kind NOT IN ('geometry_authoring_source','authoring_draft',
            'geometry_inspection','geometry_inspection_request','geometry_preview_case_report')$after$);
      END $patch$;
    """))
    op.execute(sa.text("""
      DO $patch$ DECLARE definition text; BEGIN
        SELECT pg_get_functiondef('cad2bim_geometry_operation_catalog_visible(text,text,text)'::regprocedure)
          INTO definition;
        IF position($before$operation.operation_kind <> 'geometry_resource_write'$before$ IN definition)=0 THEN
          RAISE EXCEPTION 'Expected Operation visibility guard is absent' USING ERRCODE='23514'; END IF;
        EXECUTE replace(definition,$before$operation.operation_kind <> 'geometry_resource_write'$before$,
          $after$operation.operation_kind NOT IN ('geometry_resource_write','authoring_publish','geometry_inspection')$after$);
      END $patch$;
    """))
    op.execute(sa.text("GRANT UPDATE (inspection_result_artifact_id,inspection_result_artifact_version_id,"
                       "inspection_result_artifact_sha256) ON critical_operations TO cad2bim_system_curator"))


def upgrade():
    _root_drafts()
    _operation_checks()
    _short_operations()
    _inspection_guard()
    _private_artifacts()
    _replace_check("reference_kind_valid", "reference_kind IN ('gr','gv','fr','fv','sc','gp','pp','gq','gi','gd','fd')", "opaque_reference_index")


def downgrade():
    raise RuntimeError("authoring draft migration is irreversible; restore the pre-cutover database backup")
