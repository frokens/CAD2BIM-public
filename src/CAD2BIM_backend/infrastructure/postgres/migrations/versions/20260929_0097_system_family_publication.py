"""Admit curator-only SYSTEM exact copies on the existing Family Operation.

Revision ID: 20260929_0097
Revises: 20260928_0096
"""

from importlib import import_module

from alembic import op
import sqlalchemy as sa

revision = "20260929_0097"
down_revision = "20260928_0096"
branch_labels = None
depends_on = None

_ROLE = "cad2bim_system_curator"
_CHECK = "ck_critical_operations_operation_kind_detail_valid"
_FAMILY_TABLES = (
    "product_families", "product_family_versions",
    "product_family_geometry_bindings", "product_family_version_assets",
)
_MANAGEMENT_READ = (
    "users", "tenant_principals", "tasks", "task_tenant_bindings", "runs",
    "artifacts", "artifact_versions", "critical_operations", "data_catalog_entries",
)
_MANAGEMENT_INSERT = _MANAGEMENT_READ[2:]
_OPERATION_UPDATE = (
    "status, result_product_family_version_id, error_code, error_message, error_phase, "
    "finished_at, authoring_execution_owner, authoring_execution_generation, "
    "authoring_execution_token_sha256"
)
_MANAGEMENT_FUNCTIONS = (
    "cad2bim_resolve_identity(text,text,text,text)",
    "cad2bim_product_family_source_artifact_visible(text,text,text)",
    "cad2bim_product_family_operation_catalog_visible(text,text,text)",
    "cad2bim_geometry_source_artifact_visible(text,text,text)",
    "cad2bim_geometry_operation_catalog_visible(text,text,text)",
    "cad2bim_ifc_query_artifact_visible(text,text,text)",
    "cad2bim_data_query_artifact_visible(text,text,text)",
)


def _detail_check(*, system_copy: bool) -> str:
    previous = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260920_0088_retire_ifc_products_apply"
    )
    detail = previous._operation_detail_check(products=False)
    clause = (
        "(operation_detail_json #>> '{version_draft,abi_profile}' = "
        "'product_realization_v3' AND (operation_detail_json #>> "
        "'{version_draft,family_contract,schema}' = 'product_family_contract_v3') IS TRUE)"
    )
    if detail.count(clause) != 1:
        raise RuntimeError("Current Family ABI check marker changed")
    detail = detail.replace(clause, clause + " OR " + clause.replace("_v3", "_v4"), 1)
    if system_copy:
        modes = "('create', 'repair_attempt', 'revise_version')"
        create = "operation_detail_json ->> 'mode' = 'create'"
        if detail.count(modes) != 1 or detail.count(create) != 1:
            raise RuntimeError("Current Family mode check marker changed")
        detail = detail.replace(modes, "('create', 'repair_attempt', 'revise_version', 'system_copy')", 1)
        detail = detail.replace(create, "operation_detail_json ->> 'mode' IN ('create', 'system_copy')", 1)
    return detail


_COPY_ADMISSION = """
    IF NEW.operation_kind = 'product_family_write'
       AND NEW.operation_detail_json ->> 'mode' = 'system_copy' THEN
        IF NOT pg_has_role(session_user, 'cad2bim_system_curator', 'MEMBER')
           OR NEW.operation_detail_json #>> '{system_copy,target_owner,scope_kind}' IS DISTINCT FROM 'system'
           OR NEW.operation_detail_json #>> '{system_copy,source_owner_scope,scope_kind}' IS DISTINCT FROM 'task'
           OR NEW.operation_detail_json #>> '{system_copy,source_kind}' NOT IN ('authorized_catalog','selected_library')
           OR NEW.operation_detail_json #>> '{version_draft,parent_family_version_id}' IS NOT NULL
           OR NEW.base_product_family_version_id IS NOT NULL
           OR NEW.repair_parent_operation_id IS NOT NULL
           OR NEW.target_product_family_id = NEW.operation_detail_json #>> '{system_copy,source_family_id}'
           OR jsonb_typeof(NEW.operation_detail_json #> '{system_copy,geometry_copies}') IS DISTINCT FROM 'array'
           OR jsonb_typeof(NEW.operation_detail_json #> '{system_copy,family_assets}') IS DISTINCT FROM 'array'
        THEN
            RAISE EXCEPTION 'SYSTEM Family copy requires curator authority and a closed exact plan'
                USING ERRCODE = '23514';
        END IF;
    END IF;
"""


def _operation_policy(*, system_copy: bool) -> None:
    membership = (
        "pg_has_role(session_user, 'cad2bim_mcp_runtime', 'MEMBER') OR "
        "pg_has_role(session_user, 'cad2bim_runner_runtime', 'MEMBER')"
    )
    if system_copy:
        membership += (
            " OR (pg_has_role(session_user, 'cad2bim_system_curator', 'MEMBER') "
            "AND operation_detail_json ->> 'mode' = 'system_copy')"
        )
    op.execute(sa.text("DROP POLICY product_family_authoring_task_isolation ON critical_operations"))
    op.execute(sa.text(
        "CREATE POLICY product_family_authoring_task_isolation ON critical_operations "
        "AS RESTRICTIVE USING (operation_kind <> 'product_family_write' OR "
        "task_id = NULLIF(current_setting('cad2bim.task_id', true), '')) "
        "WITH CHECK (operation_kind <> 'product_family_write' OR ((" + membership +
        ") AND task_id = NULLIF(current_setting('cad2bim.task_id', true), '')))"
    ))
    op.execute(sa.text("DROP POLICY product_family_source_task_isolation ON artifacts"))
    source_membership = (
        "pg_has_role(session_user, 'cad2bim_mcp_runtime', 'MEMBER') OR "
        "pg_has_role(session_user, 'cad2bim_runner_runtime', 'MEMBER')"
    )
    if system_copy:
        source_membership += " OR pg_has_role(session_user, 'cad2bim_system_curator', 'MEMBER')"
    op.execute(sa.text(
        "CREATE POLICY product_family_source_task_isolation ON artifacts AS RESTRICTIVE "
        "USING (kind <> 'product_family_authoring_source' OR "
        "task_id = NULLIF(current_setting('cad2bim.task_id', true), '')) "
        "WITH CHECK (kind <> 'product_family_authoring_source' OR ((" + source_membership +
        ") AND task_id = NULLIF(current_setting('cad2bim.task_id', true), '')))"
    ))


def upgrade() -> None:
    schema = op.get_bind().dialect.identifier_preparer.quote(
        op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
    )
    op.drop_constraint(op.f(_CHECK), "critical_operations", type_="check")
    op.create_check_constraint(op.f(_CHECK), "critical_operations", _detail_check(system_copy=True))
    guards = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260920_0087_ephemeral_authoring_validation"
    )
    guards._replace_function("cad2bim_guard_product_family_authoring_operation", "BEGIN\n", "BEGIN\n" + _COPY_ADMISSION)
    _operation_policy(system_copy=True)

    for table in _MANAGEMENT_READ:
        op.execute(sa.text(f"GRANT SELECT ON TABLE {table} TO {_ROLE}"))
    for table in _MANAGEMENT_INSERT:
        op.execute(sa.text(f"GRANT INSERT ON TABLE {table} TO {_ROLE}"))
    op.execute(sa.text(f"GRANT UPDATE ON TABLE artifacts TO {_ROLE}"))
    op.execute(sa.text(f"GRANT UPDATE (status) ON TABLE tasks, runs TO {_ROLE}"))
    op.execute(sa.text(f"GRANT UPDATE ({_OPERATION_UPDATE}) ON TABLE critical_operations TO {_ROLE}"))
    for function in _MANAGEMENT_FUNCTIONS:
        op.execute(sa.text(f"GRANT EXECUTE ON FUNCTION {schema}.{function} TO {_ROLE}"))

    restrictions = {
        "tasks": "active_module = 'system_library'",
        "runs": "module = 'system_library' AND stage_key = 'publication' AND execution_mode = 'direct_mcp'",
        "task_tenant_bindings": "EXISTS (SELECT 1 FROM tasks WHERE tasks.task_id = task_tenant_bindings.task_id AND tasks.active_module = 'system_library')",
        "artifacts": "kind = 'product_family_authoring_source' AND task_id = NULLIF(current_setting('cad2bim.task_id', true), '')",
        "artifact_versions": f"{schema}.cad2bim_product_family_source_artifact_visible(owner_id, task_id, artifact_id)",
        "critical_operations": "operation_kind = 'product_family_write' AND operation_detail_json ->> 'mode' = 'system_copy'",
        "data_catalog_entries": "resource_kind = 'operation'",
    }
    for table, expression in restrictions.items():
        op.execute(sa.text(
            f"CREATE POLICY system_family_management ON {table} AS RESTRICTIVE TO {_ROLE} "
            f"USING ({expression}) WITH CHECK ({expression})"
        ))
    root = "owner_scope = 'SYSTEM'"
    predicates = {
        "product_families": root,
        "product_family_versions": "EXISTS (SELECT 1 FROM product_families f WHERE f.family_id = product_family_versions.family_id AND f.owner_scope = 'SYSTEM')",
        "product_family_geometry_bindings": "EXISTS (SELECT 1 FROM product_family_versions v JOIN product_families f ON f.family_id = v.family_id WHERE v.family_version_id = product_family_geometry_bindings.family_version_id AND f.owner_scope = 'SYSTEM')",
        "product_family_version_assets": "EXISTS (SELECT 1 FROM product_family_versions v JOIN product_families f ON f.family_id = v.family_id WHERE v.family_version_id = product_family_version_assets.family_version_id AND f.owner_scope = 'SYSTEM')",
    }
    for table, expression in predicates.items():
        op.execute(sa.text(f"GRANT INSERT ON TABLE {table} TO {_ROLE}"))
        op.execute(sa.text(
            f"CREATE POLICY {table}_system_insert ON {table} FOR INSERT TO {_ROLE} "
            f"WITH CHECK ({expression})"
        ))


def downgrade() -> None:
    op.execute(sa.text("""DO $guard$ BEGIN
        IF EXISTS (SELECT 1 FROM critical_operations WHERE operation_detail_json ->> 'mode' = 'system_copy') THEN
            RAISE EXCEPTION 'Cannot remove retained SYSTEM publication Operations' USING ERRCODE = '23514';
        END IF;
    END $guard$"""))
    guards = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260920_0087_ephemeral_authoring_validation"
    )
    guards._replace_function("cad2bim_guard_product_family_authoring_operation", _COPY_ADMISSION, "")
    _operation_policy(system_copy=False)
    for table in ("tasks", "runs", "task_tenant_bindings", "artifacts", "artifact_versions", "critical_operations", "data_catalog_entries"):
        op.execute(sa.text(f"DROP POLICY system_family_management ON {table}"))
    for table in _FAMILY_TABLES:
        op.execute(sa.text(f"DROP POLICY {table}_system_insert ON {table}"))
        op.execute(sa.text(f"REVOKE INSERT ON TABLE {table} FROM {_ROLE}"))
    for table in _MANAGEMENT_READ:
        op.execute(sa.text(f"REVOKE SELECT ON TABLE {table} FROM {_ROLE}"))
    for table in _MANAGEMENT_INSERT:
        op.execute(sa.text(f"REVOKE INSERT ON TABLE {table} FROM {_ROLE}"))
    op.execute(sa.text(f"REVOKE UPDATE ON TABLE artifacts FROM {_ROLE}"))
    op.execute(sa.text(f"REVOKE UPDATE (status) ON TABLE tasks, runs FROM {_ROLE}"))
    op.execute(sa.text(f"REVOKE UPDATE ({_OPERATION_UPDATE}) ON TABLE critical_operations FROM {_ROLE}"))
    schema = op.get_bind().exec_driver_sql("SELECT current_schema()").scalar_one()
    for function in _MANAGEMENT_FUNCTIONS:
        op.execute(sa.text(f"REVOKE EXECUTE ON FUNCTION {schema}.{function} FROM {_ROLE}"))
    op.drop_constraint(op.f(_CHECK), "critical_operations", type_="check")
    op.create_check_constraint(op.f(_CHECK), "critical_operations", _detail_check(system_copy=False))
