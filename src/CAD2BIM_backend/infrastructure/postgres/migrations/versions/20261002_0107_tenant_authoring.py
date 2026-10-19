"""Authorize tenant authoring through the existing roots and exact snapshots.

Revision ID: 20261002_0107
Revises: 20261002_0106
"""

from alembic import context, op
import sqlalchemy as sa

revision = "20261002_0107"
down_revision = "20261002_0106"
branch_labels = None
depends_on = None

_WRITE_ROLES = "cad2bim_mcp_runtime,cad2bim_runner_runtime"
_DRAFT_TYPE = "application/vnd.cad2bim.authoring-draft+json"


def _schema():
    if context.is_offline_mode():
        return "public"
    bind = op.get_bind()
    return bind.dialect.identifier_preparer.quote(
        bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    )


def _owned(alias=""):
    prefix = alias + "." if alias else ""
    return (
        f"{prefix}owner_tenant_id=NULLIF(current_setting('cad2bim.tenant_id',true),'') "
        "AND NULLIF(current_setting('cad2bim.task_id',true),'') IS NOT NULL AND ("
        f"({prefix}owner_scope='TENANT' AND {prefix}owner_task_id IS NULL) OR "
        f"({prefix}owner_scope='TASK' AND {prefix}owner_task_id="
        "NULLIF(current_setting('cad2bim.task_id',true),'')))"
    )


def _tenant_roots(schema):
    return (
        f"SELECT 'geometry'::text AS root_kind,resource_id AS root_id,"
        f"draft_generation,draft_artifact_version_id FROM {schema}.geometry_resources "
        "WHERE owner_scope='TENANT' AND owner_tenant_id="
        "NULLIF(current_setting('cad2bim.tenant_id',true),'') UNION ALL "
        f"SELECT 'family'::text,family_id,draft_generation,draft_artifact_version_id "
        f"FROM {schema}.product_families WHERE owner_scope='TENANT' AND owner_tenant_id="
        "NULLIF(current_setting('cad2bim.tenant_id',true),'')"
    )


def _runtime_write_policies():
    for table in ("geometry_resources", "product_families"):
        op.execute(sa.text(f"DROP POLICY {table}_runtime_insert ON {table}"))
        op.execute(sa.text(f"CREATE POLICY {table}_runtime_insert ON {table} FOR INSERT "
                           f"TO {_WRITE_ROLES} WITH CHECK ({_owned()})"))
        op.execute(sa.text(f"DROP POLICY {table}_draft_task_update ON {table}"))
        op.execute(sa.text(f"CREATE POLICY {table}_draft_task_update ON {table} FOR UPDATE "
                           f"TO {_WRITE_ROLES} USING ({_owned()}) WITH CHECK ({_owned()})"))

    for table, owner in (
        ("geometry_resource_versions", "SELECT 1 FROM geometry_resources r WHERE "
         "r.resource_id=geometry_resource_versions.resource_id"),
        ("geometry_resource_version_assets", "SELECT 1 FROM geometry_resource_versions v "
         "JOIN geometry_resources r ON r.resource_id=v.resource_id WHERE "
         "v.resource_version_id=geometry_resource_version_assets.resource_version_id"),
        ("product_family_versions", "SELECT 1 FROM product_families r WHERE "
         "r.family_id=product_family_versions.family_id"),
        ("product_family_geometry_bindings", "SELECT 1 FROM product_family_versions v "
         "JOIN product_families r ON r.family_id=v.family_id WHERE "
         "v.family_version_id=product_family_geometry_bindings.family_version_id"),
        ("product_family_version_assets", "SELECT 1 FROM product_family_versions v "
         "JOIN product_families r ON r.family_id=v.family_id WHERE "
         "v.family_version_id=product_family_version_assets.family_version_id"),
    ):
        op.execute(sa.text(f"DROP POLICY {table}_runtime_insert ON {table}"))
        op.execute(sa.text(f"CREATE POLICY {table}_runtime_insert ON {table} FOR INSERT "
                           f"TO {_WRITE_ROLES} WITH CHECK (EXISTS ({owner} AND {_owned('r')}))"))

    op.execute(sa.text("DROP POLICY geometry_inputs_task_insert ON geometry_resource_version_inputs"))
    op.execute(sa.text(f"""
        CREATE POLICY geometry_inputs_task_insert ON geometry_resource_version_inputs
        FOR INSERT TO {_WRITE_ROLES} WITH CHECK (EXISTS (
          SELECT 1 FROM geometry_resource_versions v
          JOIN geometry_resources r ON r.resource_id=v.resource_id
          JOIN geometry_resource_versions target ON target.resource_version_id=
            geometry_resource_version_inputs.target_resource_version_id
          JOIN geometry_resources target_root ON target_root.resource_id=target.resource_id
          JOIN geometry_resource_version_assets asset ON asset.resource_version_id=target.resource_version_id
          WHERE v.resource_version_id=geometry_resource_version_inputs.resource_version_id
          AND {_owned('r')}
          AND target_root.resource_kind='mapped_model'
          AND asset.asset_role='canonical_geometry' AND asset.asset_format='occt_brep_v1'
          AND (target_root.owner_scope='SYSTEM' OR (target_root.owner_scope='TENANT'
            AND target_root.owner_tenant_id=r.owner_tenant_id) OR (r.owner_scope='TASK'
            AND target_root.owner_scope='TASK' AND target_root.owner_tenant_id=r.owner_tenant_id
            AND target_root.owner_task_id=r.owner_task_id))
        ))
    """))


def _lookup_functions(schema):
    roots = _tenant_roots(schema)
    op.execute(sa.text(f"""
      CREATE FUNCTION {schema}.cad2bim_authoring_tenant_snapshot_visible(p_version_id text)
      RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
      SET search_path={schema},pg_temp SET row_security=off AS $function$
        SELECT EXISTS (
          SELECT 1 FROM {schema}.artifact_versions v JOIN {schema}.artifacts a ON a.artifact_id=v.artifact_id
          WHERE v.version_id=p_version_id AND v.tenant_id=NULLIF(current_setting('cad2bim.tenant_id',true),'')
          AND a.kind='authoring_draft' AND a.status IN ('ready','available')
          AND v.content_type='{_DRAFT_TYPE}' AND (
            EXISTS (SELECT 1 FROM ({roots}) r WHERE r.draft_artifact_version_id=v.version_id)
            OR EXISTS (SELECT 1 FROM {schema}.critical_operations o
              CROSS JOIN LATERAL jsonb_array_elements(o.operation_detail_json->'expected_drafts') item
              JOIN ({roots}) r ON r.root_kind=item->>'root_kind' AND r.root_id=item->>'root_id'
              WHERE o.tenant_id=v.tenant_id AND o.operation_kind='authoring_publish' AND o.status='prepared'
              AND item#>>'{{snapshot,version_id}}'=v.version_id
              AND item#>>'{{snapshot,artifact_id}}'=v.artifact_id AND item#>>'{{snapshot,sha256}}'=v.sha256)
          )
        )
      $function$;
      CREATE FUNCTION {schema}.cad2bim_authoring_publication_fence(p_root_kind text,p_root_id text)
      RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
      SET search_path={schema},pg_temp SET row_security=off AS $function$
        SELECT EXISTS (SELECT 1 FROM ({roots}) r JOIN {schema}.critical_operations o
          ON o.tenant_id=NULLIF(current_setting('cad2bim.tenant_id',true),'')
          CROSS JOIN LATERAL jsonb_array_elements(o.operation_detail_json->'expected_drafts') item
          WHERE r.root_kind=p_root_kind AND r.root_id=p_root_id
          AND o.operation_kind='authoring_publish' AND o.status='prepared'
          AND item->>'root_kind'=r.root_kind AND item->>'root_id'=r.root_id)
      $function$;
      CREATE FUNCTION {schema}.cad2bim_authoring_publication_result(
        p_root_kind text,p_root_id text,p_expected_drafts jsonb)
      RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
      SET search_path={schema},pg_temp SET row_security=off AS $function$
        SELECT jsonb_build_object('geometry_versions',o.operation_detail_json->'geometry_versions',
          'family_version',CASE WHEN o.operation_detail_json->'family_version'='null'::jsonb THEN NULL
            ELSE jsonb_build_object('family_id',o.operation_detail_json#>>'{{family_version,family_id}}',
              'family_version_id',o.operation_detail_json#>>'{{family_version,family_version_id}}') END)
        FROM {schema}.critical_operations o
        WHERE o.tenant_id=NULLIF(current_setting('cad2bim.tenant_id',true),'')
        AND o.operation_kind='authoring_publish' AND o.status='succeeded'
        AND o.operation_detail_json ? 'preview_examples'
        AND o.operation_detail_json->>'root_kind'=p_root_kind AND o.operation_detail_json->>'root_id'=p_root_id
        AND o.operation_detail_json->'expected_drafts' @> p_expected_drafts
        AND o.operation_detail_json->'expected_drafts' <@ p_expected_drafts
        AND jsonb_array_length(p_expected_drafts)>0
        AND EXISTS (SELECT 1 FROM ({roots}) r WHERE r.root_kind=p_root_kind AND r.root_id=p_root_id)
        AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(p_expected_drafts) item WHERE NOT EXISTS (
          SELECT 1 FROM ({roots}) r JOIN {schema}.artifact_versions v ON v.version_id=r.draft_artifact_version_id
          WHERE r.root_kind=item->>'root_kind' AND r.root_id=item->>'root_id'
          AND r.draft_generation=(item->>'generation')::bigint
          AND v.version_id=item#>>'{{snapshot,version_id}}' AND v.artifact_id=item#>>'{{snapshot,artifact_id}}'
          AND v.sha256=item#>>'{{snapshot,sha256}}'))
        ORDER BY o.started_at DESC LIMIT 1
      $function$;
      CREATE FUNCTION {schema}.cad2bim_authoring_geometry_draft_result(
        p_root_id text,p_generation bigint,p_snapshot jsonb)
      RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
      SET search_path={schema},pg_temp SET row_security=off AS $function$
        SELECT result.value FROM {schema}.critical_operations o
        CROSS JOIN LATERAL jsonb_array_elements(o.operation_detail_json->'geometry_versions') result(value)
        WHERE o.tenant_id=NULLIF(current_setting('cad2bim.tenant_id',true),'')
        AND o.operation_kind='authoring_publish' AND o.status='succeeded'
        AND o.operation_detail_json ? 'preview_examples' AND result.value->>'resource_id'=p_root_id
        AND o.operation_detail_json->'expected_drafts' @> jsonb_build_array(jsonb_build_object(
          'root_kind','geometry','root_id',p_root_id,'generation',p_generation,'snapshot',p_snapshot))
        AND EXISTS (SELECT 1 FROM ({roots}) r JOIN {schema}.artifact_versions v ON v.version_id=r.draft_artifact_version_id
          WHERE r.root_kind='geometry' AND r.root_id=p_root_id AND r.draft_generation=p_generation
          AND v.version_id=p_snapshot->>'version_id' AND v.artifact_id=p_snapshot->>'artifact_id'
          AND v.sha256=p_snapshot->>'sha256')
        ORDER BY o.started_at DESC LIMIT 1
      $function$;
    """))
    for signature in (
        "cad2bim_authoring_tenant_snapshot_visible(text)",
        "cad2bim_authoring_publication_fence(text,text)",
        "cad2bim_authoring_publication_result(text,text,jsonb)",
        "cad2bim_authoring_geometry_draft_result(text,bigint,jsonb)",
    ):
        op.execute(sa.text(f"REVOKE ALL ON FUNCTION {schema}.{signature} FROM PUBLIC"))
        roles = _WRITE_ROLES
        if signature.startswith("cad2bim_authoring_tenant_snapshot_visible"):
            roles += ",cad2bim_api_runtime,cad2bim_system_curator"
        op.execute(sa.text(f"GRANT EXECUTE ON FUNCTION {schema}.{signature} TO {roles}"))


def _replace_function(name, before, after):
    signature = name if "(" in name else name + "()"
    bind = op.get_bind()
    definition = bind.execute(sa.text(
        "SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"
    ), {"signature": signature}).scalar_one()
    if definition.count(before) != 1:
        raise RuntimeError(f"Expected one authoring guard fragment in {name}")
    bind.execute(sa.text(definition.replace(before, after)))


def _snapshot_policies(schema):
    task = "task_id=NULLIF(current_setting('cad2bim.task_id',true),'')"
    roles = "(pg_has_role(session_user,'cad2bim_mcp_runtime','MEMBER') OR " \
            "pg_has_role(session_user,'cad2bim_runner_runtime','MEMBER') OR " \
            "pg_has_role(session_user,'cad2bim_system_curator','MEMBER'))"
    kinds = "('geometry_inspection','geometry_inspection_request','geometry_preview_case_report','authoring_draft')"
    op.execute(sa.text("DROP POLICY geometry_inspection_asset_task_isolation ON artifacts"))
    op.execute(sa.text(f"""
      CREATE POLICY geometry_inspection_asset_task_isolation ON artifacts AS RESTRICTIVE
      USING (kind NOT IN {kinds} OR {task} OR (kind='authoring_draft' AND EXISTS (
        SELECT 1 FROM artifact_versions v WHERE v.artifact_id=artifacts.artifact_id
        AND {schema}.cad2bim_authoring_tenant_snapshot_visible(v.version_id))))
      WITH CHECK (kind NOT IN {kinds} OR ({task} AND {roles}))
    """))
    # Artifact and version policies must not query one another recursively.
    # The existing security-definer source lookup resolves the exact root grant.
    _replace_function("cad2bim_geometry_source_artifact_visible(text,text,text)",
        "artifact.kind NOT IN ('geometry_authoring_source','authoring_draft',\n"
        "            'geometry_inspection','geometry_inspection_request','geometry_preview_case_report')",
        "(artifact.kind NOT IN ('geometry_authoring_source','authoring_draft',\n"
        "            'geometry_inspection','geometry_inspection_request','geometry_preview_case_report')\n"
        f" OR (artifact.kind='authoring_draft' AND EXISTS (SELECT 1 FROM {schema}.artifact_versions v "
        f"WHERE v.artifact_id=artifact.artifact_id AND {schema}.cad2bim_authoring_tenant_snapshot_visible(v.version_id))))")
    op.execute(sa.text(f"""
      CREATE POLICY authoring_draft_exact_version_isolation ON artifact_versions AS RESTRICTIVE
      USING (content_type<>'{_DRAFT_TYPE}' OR {task}
        OR {schema}.cad2bim_authoring_tenant_snapshot_visible(version_id))
      WITH CHECK (content_type<>'{_DRAFT_TYPE}' OR ({task} AND {roles}))
    """))


def _authoring_guards(schema):
    _replace_function("cad2bim_guard_authoring_draft_root",
        "OR (NEW.owner_scope='SYSTEM' AND pg_has_role(session_user,'cad2bim_system_curator','MEMBER'))",
        "OR (NEW.owner_scope='TENANT' AND NEW.owner_tenant_id=NULLIF(current_setting('cad2bim.tenant_id',true),'') "
        "AND NEW.owner_task_id IS NULL AND NULLIF(current_setting('cad2bim.task_id',true),'') IS NOT NULL)\n"
        "OR (NEW.owner_scope='SYSTEM' AND pg_has_role(session_user,'cad2bim_system_curator','MEMBER'))")
    _replace_function("cad2bim_guard_authoring_publication_operation",
        "OR (current_scope='SYSTEM' AND pg_has_role(session_user,'cad2bim_system_curator','MEMBER'))",
        "OR (current_scope='TENANT' AND tenant=NEW.tenant_id AND task IS NULL)\n"
        "OR (current_scope='SYSTEM' AND pg_has_role(session_user,'cad2bim_system_curator','MEMBER'))")
    _replace_function("cad2bim_guard_authoring_publication_operation",
        "SELECT 1 FROM artifact_versions v WHERE v.owner_id=NEW.owner_id AND v.tenant_id=NEW.tenant_id\n"
        "          AND v.task_id=NEW.task_id AND v.version_id=snapshot_id",
        "SELECT 1 FROM artifact_versions v JOIN artifacts a ON a.artifact_id=v.artifact_id\n"
        "          WHERE v.tenant_id=NEW.tenant_id AND (current_scope='TENANT' OR "
        "(v.owner_id=NEW.owner_id AND v.task_id=NEW.task_id))\n"
        f"          AND a.kind='authoring_draft' AND a.status IN ('ready','available') AND v.content_type='{_DRAFT_TYPE}'\n"
        "          AND v.version_id=snapshot_id")
    _replace_function("cad2bim_guard_authoring_publication_operation",
        "FOR item IN SELECT value FROM jsonb_array_elements(NEW.operation_detail_json->'expected_drafts') LOOP",
        "FOR item IN SELECT value FROM jsonb_array_elements(NEW.operation_detail_json->'expected_drafts')\n"
        "        ORDER BY value->>'root_id',value->>'root_kind' LOOP\n"
        "        PERFORM pg_advisory_xact_lock(hashtextextended(item->>'root_id',0));\n"
        f"        IF TG_OP='INSERT' AND {schema}.cad2bim_authoring_publication_fence(item->>'root_kind',item->>'root_id') THEN\n"
        "          RAISE EXCEPTION 'tenant draft has an unresolved publication' USING ERRCODE='23514'; END IF;")
    _replace_function("cad2bim_guard_geometry_inspection_operation",
        "v.owner_id=NEW.owner_id AND v.tenant_id=NEW.tenant_id\n"
        "        AND v.task_id=NEW.task_id AND v.artifact_id=NEW.operation_detail_json#>>'{source_draft_artifact,artifact_id}'",
        "v.tenant_id=NEW.tenant_id AND ((v.owner_id=NEW.owner_id AND v.task_id=NEW.task_id) OR "
        f"{schema}.cad2bim_authoring_tenant_snapshot_visible(v.version_id))\n"
        "        AND v.artifact_id=NEW.operation_detail_json#>>'{source_draft_artifact,artifact_id}'")


def upgrade():
    schema = _schema()
    _runtime_write_policies()
    _lookup_functions(schema)
    _snapshot_policies(schema)
    _authoring_guards(schema)


def downgrade():
    raise RuntimeError("Restore the database backup before tenant authoring; shared roots cannot be downgraded")
