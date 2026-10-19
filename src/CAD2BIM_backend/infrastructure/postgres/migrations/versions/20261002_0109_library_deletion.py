"""Delete catalog roots from online use without erasing immutable history.

Revision ID: 20261002_0109
Revises: 20261002_0108
"""

from alembic import op
import sqlalchemy as sa

revision = "20261002_0109"
down_revision = "20261002_0108"
branch_labels = None
depends_on = None


def _replace_function(signature, before, after):
    bind = op.get_bind()
    definition = bind.execute(sa.text(
        "SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"
    ), {"signature": signature}).scalar_one()
    if definition.count(before) != 1:
        raise RuntimeError(f"Expected one lifecycle guard fragment in {signature}")
    bind.execute(sa.text(definition.replace(before, after)))


def upgrade():
    bind = op.get_bind()
    schema = bind.dialect.identifier_preparer.quote(bind.execute(sa.text("SELECT current_schema()")).scalar_one())
    for table in ("geometry_resources", "product_families"):
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        op.create_check_constraint(op.f(f"ck_{table}_system_not_deleted"), table,
                                   "owner_scope <> 'SYSTEM' OR deleted_at IS NULL")
        op.execute(sa.text(f"CREATE POLICY {table}_live ON {table} AS RESTRICTIVE "
                           "USING (deleted_at IS NULL) WITH CHECK (deleted_at IS NULL)"))

    before = "IF TG_OP='UPDATE' AND (to_jsonb(NEW)-ARRAY['draft_artifact_version_id','draft_generation'])"
    _replace_function("cad2bim_guard_authoring_draft_root()", before, """
        IF TG_OP='INSERT' AND NEW.deleted_at IS NOT NULL THEN
          RAISE EXCEPTION 'new library roots cannot be deleted' USING ERRCODE='23514';
        END IF;
        IF TG_OP='UPDATE' AND OLD.deleted_at IS NOT NULL THEN
          RAISE EXCEPTION 'deleted library roots are immutable' USING ERRCODE='23514';
        END IF;
        IF TG_OP='UPDATE' AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at THEN
          IF NEW.owner_scope='SYSTEM' OR NEW.deleted_at IS NULL OR
              (to_jsonb(NEW)-'deleted_at') IS DISTINCT FROM (to_jsonb(OLD)-'deleted_at') THEN
            RAISE EXCEPTION 'library deletion cannot change resource identity' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END IF;
        """ + before)
    _replace_function("cad2bim_check_authoring_draft_commit()",
        "IF NEW.draft_artifact_version_id IS NULL THEN RETURN NEW; END IF;",
        "IF TG_OP='UPDATE' AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at THEN RETURN NEW; END IF;\n"
        "        IF NEW.draft_artifact_version_id IS NULL THEN RETURN NEW; END IF;")

    # Keep the existing exact-parent/scope/digest binding guard. Acquiring the
    # same root lock as deletion before its query closes the reference race.
    _replace_function("cad2bim_guard_product_family_geometry_binding()",
        "BEGIN\n                SELECT",
        "BEGIN\n                PERFORM pg_advisory_xact_lock(hashtextextended(NEW.geometry_resource_id,0));\n"
        "                SELECT")
    _replace_function("cad2bim_guard_product_family_geometry_binding()",
        "WHERE family_version.family_version_id = NEW.family_version_id;",
        "WHERE family_version.family_version_id = NEW.family_version_id\n"
        "                  AND family.deleted_at IS NULL AND geometry.deleted_at IS NULL;")

    op.execute(sa.text(f"""
      CREATE FUNCTION {schema}.cad2bim_guard_library_version_parent()
      RETURNS trigger LANGUAGE plpgsql AS $guard$
      DECLARE root_id text; live boolean;
      BEGIN
        root_id := CASE WHEN TG_TABLE_NAME='geometry_resource_versions' THEN to_jsonb(NEW)->>'resource_id' ELSE to_jsonb(NEW)->>'family_id' END;
        PERFORM pg_advisory_xact_lock(hashtextextended(root_id,0));
        IF TG_TABLE_NAME='geometry_resource_versions' THEN
          SELECT deleted_at IS NULL INTO live FROM geometry_resources WHERE resource_id=root_id;
        ELSE
          SELECT deleted_at IS NULL INTO live FROM product_families WHERE family_id=root_id;
        END IF;
        IF NOT FOUND OR NOT live THEN
          RAISE EXCEPTION 'library version requires a live root' USING ERRCODE='23503';
        END IF;
        RETURN NEW;
      END $guard$;
      CREATE FUNCTION {schema}.cad2bim_guard_live_geometry_input()
      RETURNS trigger LANGUAGE plpgsql AS $guard$
      DECLARE root_id text;
      BEGIN
        SELECT resource_id INTO root_id FROM geometry_resource_versions
          WHERE resource_version_id=NEW.target_resource_version_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'geometry input is unavailable' USING ERRCODE='23503'; END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended(root_id,0));
        IF NOT EXISTS (SELECT 1 FROM geometry_resources WHERE resource_id=root_id AND deleted_at IS NULL) THEN
          RAISE EXCEPTION 'geometry input requires a live root' USING ERRCODE='23503';
        END IF;
        RETURN NEW;
      END $guard$;
    """))
    for table in ("geometry_resource_versions", "product_family_versions"):
        op.execute(sa.text(f"CREATE TRIGGER trg_{table}_live_parent BEFORE INSERT ON {table} "
                           f"FOR EACH ROW EXECUTE FUNCTION {schema}.cad2bim_guard_library_version_parent()"))
    op.execute(sa.text("CREATE TRIGGER trg_geometry_inputs_live_target BEFORE INSERT ON geometry_resource_version_inputs "
                       f"FOR EACH ROW EXECUTE FUNCTION {schema}.cad2bim_guard_live_geometry_input()"))

    # The existing root remains the sole lifecycle authority. This narrow
    # function grants no general UPDATE/DELETE privilege and checks dependencies
    # across Tasks without disclosing those Tasks or their resource identities.
    op.execute(sa.text(f"""
      CREATE FUNCTION {schema}.cad2bim_delete_library_resource(p_kind text,p_id text)
      RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
      SET search_path={schema},pg_temp SET row_security=off AS $function$
      DECLARE resource_scope text; tenant text; task text; removed timestamptz;
      BEGIN
        IF p_kind NOT IN ('geometry','family') THEN RAISE EXCEPTION 'invalid resource kind' USING ERRCODE='22023'; END IF;
        IF NOT EXISTS (SELECT 1 FROM tenant_principals membership
          JOIN tenants t ON t.tenant_id=membership.tenant_id
          JOIN principals p ON p.principal_id=membership.principal_id
          WHERE membership.tenant_id=NULLIF(current_setting('cad2bim.tenant_id',true),'')
          AND membership.principal_id=NULLIF(current_setting('cad2bim.principal_id',true),'')
          AND t.status='active' AND p.status='active') THEN RETURN 'not_found'; END IF;
        IF NULLIF(current_setting('cad2bim.task_id',true),'') IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM tasks WHERE task_id=current_setting('cad2bim.task_id',true)
          AND tenant_id=current_setting('cad2bim.tenant_id',true) AND status<>'archived'
        ) THEN RETURN 'not_found'; END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended(p_id,0));
        IF p_kind='geometry' THEN
          SELECT owner_scope,owner_tenant_id,owner_task_id,deleted_at
            INTO resource_scope,tenant,task,removed FROM geometry_resources WHERE resource_id=p_id FOR UPDATE;
        ELSE
          SELECT owner_scope,owner_tenant_id,owner_task_id,deleted_at
            INTO resource_scope,tenant,task,removed FROM product_families WHERE family_id=p_id FOR UPDATE;
        END IF;
        IF NOT FOUND OR removed IS NOT NULL THEN RETURN 'not_found'; END IF;
        IF resource_scope='SYSTEM' THEN RETURN 'system_resource'; END IF;
        IF tenant IS DISTINCT FROM NULLIF(current_setting('cad2bim.tenant_id',true),'') OR
            (resource_scope='TASK' AND task IS DISTINCT FROM NULLIF(current_setting('cad2bim.task_id',true),''))
          THEN RETURN 'not_found'; END IF;
        IF EXISTS (SELECT 1 FROM critical_operations o WHERE o.tenant_id=tenant AND o.status='prepared' AND (
          (p_kind='geometry' AND o.target_geometry_resource_id=p_id) OR
          (p_kind='family' AND o.target_product_family_id=p_id) OR
          EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE(o.operation_detail_json->'expected_drafts','[]'::jsonb)) item
            WHERE item->>'root_kind'=p_kind AND item->>'root_id'=p_id))) THEN RETURN 'publication_pending'; END IF;
        IF p_kind='geometry' AND (
          EXISTS (SELECT 1 FROM product_family_geometry_bindings b
            JOIN product_family_versions v ON v.family_version_id=b.family_version_id
            JOIN product_families f ON f.family_id=v.family_id
            WHERE b.geometry_resource_id=p_id AND f.deleted_at IS NULL)
          OR EXISTS (SELECT 1 FROM geometry_resource_version_inputs i
            JOIN geometry_resource_versions target ON target.resource_version_id=i.target_resource_version_id
            JOIN geometry_resource_versions source ON source.resource_version_id=i.resource_version_id
            JOIN geometry_resources r ON r.resource_id=source.resource_id
            WHERE target.resource_id=p_id AND r.deleted_at IS NULL)
        ) THEN RETURN 'geometry_in_use'; END IF;
        IF p_kind='geometry' THEN UPDATE geometry_resources SET deleted_at=clock_timestamp() WHERE resource_id=p_id;
        ELSE UPDATE product_families SET deleted_at=clock_timestamp() WHERE family_id=p_id; END IF;
        RETURN 'deleted';
      END $function$;
      REVOKE ALL ON FUNCTION {schema}.cad2bim_delete_library_resource(text,text) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION {schema}.cad2bim_delete_library_resource(text,text)
        TO cad2bim_api_runtime,cad2bim_runner_runtime;
    """))


def downgrade():
    raise RuntimeError("Restore the database backup before library deletion; deleted roots cannot be revived")
