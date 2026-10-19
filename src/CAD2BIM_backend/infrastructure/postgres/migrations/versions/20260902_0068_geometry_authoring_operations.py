"""Extend CriticalOperation for atomic geometry resource authoring.

Revision ID: 20260902_0068
Revises: 20260902_0067
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op


revision: str = "20260902_0068"
down_revision: str | None = "20260902_0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "critical_operations"
_STATUS_CHECK = "ck_critical_operations_status_valid"
_KIND_CHECK = "ck_critical_operations_operation_kind_valid"
_DETAIL_CHECK = "ck_critical_operations_operation_kind_detail_valid"
_TERMINAL_CHECK = "ck_critical_operations_terminal_result_valid"
_ERROR_PHASE_CHECK = "ck_critical_operations_error_phase_valid"
_FINGERPRINT_INDEX = "uq_critical_operations_run_tool_fingerprint"
_WRITE_ROLES = ("cad2bim_runner_runtime", "cad2bim_mcp_runtime")
_RLS_ROLES = (
    "cad2bim_api_runtime",
    "cad2bim_runner_runtime",
    "cad2bim_mcp_runtime",
)


def _quoted_schema() -> str:
    if context.is_offline_mode():
        return "public"
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema() ")).scalar_one()
    return bind.dialect.identifier_preparer.quote(schema)


def _create_geometry_rls_helpers(schema: str) -> None:
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {schema}.cad2bim_geometry_source_artifact_visible(
                p_owner_id text,
                p_task_id text,
                p_artifact_id text
            )
            RETURNS boolean
            LANGUAGE sql
            STABLE
            SECURITY DEFINER
            SET search_path = {schema}, pg_temp
            SET row_security = off
            AS $function$
                SELECT EXISTS (
                    SELECT 1
                      FROM {schema}.artifacts AS artifact
                     WHERE artifact.owner_id = p_owner_id
                       AND artifact.task_id = p_task_id
                       AND artifact.artifact_id = p_artifact_id
                       AND artifact.tenant_id = NULLIF(
                           current_setting('cad2bim.tenant_id', true), ''
                       )
                       AND (
                           artifact.kind <> 'geometry_authoring_source'
                           OR artifact.task_id = NULLIF(
                               current_setting('cad2bim.task_id', true), ''
                           )
                       )
                )
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {schema}.cad2bim_geometry_operation_catalog_visible(
                p_owner_id text,
                p_task_id text,
                p_operation_id text
            )
            RETURNS boolean
            LANGUAGE sql
            STABLE
            SECURITY DEFINER
            SET search_path = {schema}, pg_temp
            SET row_security = off
            AS $function$
                SELECT EXISTS (
                    SELECT 1
                      FROM {schema}.critical_operations AS operation
                     WHERE operation.owner_id = p_owner_id
                       AND operation.task_id = p_task_id
                       AND operation.operation_id = p_operation_id
                       AND operation.tenant_id = NULLIF(
                           current_setting('cad2bim.tenant_id', true), ''
                       )
                       AND (
                           operation.operation_kind <> 'geometry_resource_write'
                           OR operation.task_id = NULLIF(
                               current_setting('cad2bim.task_id', true), ''
                           )
                       )
                )
            $function$
            """
        )
    )
    for function in (
        "cad2bim_geometry_source_artifact_visible(text,text,text)",
        "cad2bim_geometry_operation_catalog_visible(text,text,text)",
    ):
        op.execute(sa.text(f"REVOKE ALL ON FUNCTION {schema}.{function} FROM PUBLIC"))
        for role in _RLS_ROLES:
            op.execute(sa.text(f"GRANT EXECUTE ON FUNCTION {schema}.{function} TO {role}"))


def _drop_geometry_rls_helpers(schema: str) -> None:
    for function in (
        "cad2bim_geometry_operation_catalog_visible(text,text,text)",
        "cad2bim_geometry_source_artifact_visible(text,text,text)",
    ):
        for role in _RLS_ROLES:
            op.execute(sa.text(f"REVOKE EXECUTE ON FUNCTION {schema}.{function} FROM {role}"))
        op.execute(sa.text(f"DROP FUNCTION {schema}.{function}"))


def _detail_check(*, geometry: bool) -> str:
    base = (
        "(operation_kind = 'source_ingestion' AND tool_name = 'cad_import_source' "
        "AND source_filename IS NOT NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NULL AND code_artifact_version_id IS NULL "
        "AND code_artifact_sha256 IS NULL AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        "AND repair_parent_operation_id IS NULL "
        "AND base_ifc_revision_id IS NULL AND expected_ifc_write_epoch IS NULL"
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
        "AND base_ifc_revision_id IS NULL AND expected_ifc_write_epoch IS NULL"
    )
    ifc = (
        "(operation_kind = 'ifc_model_write' AND source_filename IS NULL "
        "AND intent_summary IS NOT NULL AND code_artifact_id IS NULL "
        "AND code_artifact_version_id IS NULL AND code_artifact_sha256 IS NULL "
        "AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        "AND repair_parent_operation_id IS NULL AND result_package_id IS NULL "
        "AND ((tool_name = 'ifc_model_initialize' AND base_ifc_revision_id IS NULL "
        "AND expected_ifc_write_epoch IS NULL) OR "
        "(tool_name <> 'ifc_model_initialize' AND base_ifc_revision_id IS NOT NULL "
        "AND expected_ifc_write_epoch IS NOT NULL))"
    )
    if not geometry:
        return f"{base}) OR {transform}) OR {ifc})"
    common_absent = (
        " AND target_geometry_resource_id IS NULL AND result_geometry_resource_version_id IS NULL)"
    )
    authoring = (
        "(operation_kind = 'geometry_resource_write' "
        "AND tool_name IN ('ifc_geometry_resource_create', "
        "'ifc_geometry_resource_repair') AND source_filename IS NULL "
        "AND intent_summary IS NOT NULL AND code_artifact_id IS NOT NULL "
        "AND code_artifact_version_id IS NOT NULL AND code_artifact_sha256 IS NOT NULL "
        "AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        "AND ((tool_name = 'ifc_geometry_resource_create' "
        "AND repair_parent_operation_id IS NULL) OR "
        "(tool_name = 'ifc_geometry_resource_repair' "
        "AND repair_parent_operation_id IS NOT NULL)) "
        "AND result_package_id IS NULL AND base_ifc_revision_id IS NULL "
        "AND expected_ifc_write_epoch IS NULL "
        "AND target_geometry_resource_id IS NOT NULL)"
    )
    return (
        f"{base}{common_absent} OR {transform}{common_absent} OR "
        f"{ifc}{common_absent} OR {authoring}"
    )


def _terminal_check(*, geometry: bool) -> str:
    if not geometry:
        return (
            "(status = 'prepared' AND finished_at IS NULL "
            "AND result_package_id IS NULL AND error_code IS NULL "
            "AND error_message IS NULL AND error_phase IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL "
            "AND error_code IS NULL AND error_message IS NULL AND error_phase IS NULL "
            "AND ((operation_kind IN ('source_ingestion', 'data_transform') "
            "AND result_package_id IS NOT NULL) OR "
            "(operation_kind = 'ifc_model_write' AND result_package_id IS NULL))) OR "
            "(status = 'failed' AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL "
            "AND result_package_id IS NULL) OR "
            "(status = 'indeterminate' AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL "
            "AND error_phase = 'reconcile' AND result_package_id IS NULL)"
        )
    return (
        "(status = 'prepared' AND finished_at IS NULL "
        "AND result_package_id IS NULL AND error_code IS NULL "
        "AND error_message IS NULL AND error_phase IS NULL "
        "AND result_geometry_resource_version_id IS NULL) OR "
        "(status = 'succeeded' AND finished_at IS NOT NULL "
        "AND error_code IS NULL AND error_message IS NULL AND error_phase IS NULL "
        "AND ((operation_kind IN ('source_ingestion', 'data_transform') "
        "AND result_package_id IS NOT NULL "
        "AND result_geometry_resource_version_id IS NULL) OR "
        "(operation_kind = 'ifc_model_write' AND result_package_id IS NULL "
        "AND result_geometry_resource_version_id IS NULL) OR "
        "(operation_kind = 'geometry_resource_write' AND result_package_id IS NULL "
        "AND result_geometry_resource_version_id IS NOT NULL))) OR "
        "(status = 'failed' AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL AND error_message IS NOT NULL "
        "AND result_package_id IS NULL "
        "AND result_geometry_resource_version_id IS NULL) OR "
        "(status = 'indeterminate' AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL AND error_message IS NOT NULL "
        "AND error_phase = 'reconcile' AND result_package_id IS NULL "
        "AND result_geometry_resource_version_id IS NULL)"
    )


def _error_phase_check(*, geometry: bool) -> str:
    base = (
        "(status IN ('prepared', 'succeeded') AND error_phase IS NULL) OR "
        "(status = 'indeterminate' AND error_phase = 'reconcile') OR "
        "(status = 'failed' AND operation_kind = 'source_ingestion' "
        "AND (error_phase IS NULL OR error_phase = 'source_extraction')) OR "
        "(status = 'failed' AND operation_kind = 'data_transform' "
        "AND error_phase IN ('code_validation', 'execution', 'output_validation')) OR "
        "(status = 'failed' AND operation_kind = 'ifc_model_write' "
        "AND error_phase IN ('ifc_validation', 'storage_verification', 'commit_fence'))"
    )
    if not geometry:
        return base
    return (
        f"{base} OR (status = 'failed' "
        "AND operation_kind = 'geometry_resource_write' "
        "AND error_phase IN ('code_validation', 'execution', 'output_validation', "
        "'storage_verification', 'commit_fence'))"
    )


def _create_authoring_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_guard_geometry_authoring_operation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
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
                    IF NOT (
                        (OLD.status = 'prepared'
                         AND NEW.status IN ('succeeded', 'failed', 'indeterminate'))
                        OR (OLD.status = 'indeterminate' AND NEW.status = 'prepared')
                    ) THEN
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
                RETURN NEW;
            END
            $guard$
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_geometry_authoring_operation_guard "
            "BEFORE INSERT OR UPDATE ON critical_operations "
            "FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_geometry_authoring_operation()"
        )
    )


def upgrade() -> None:
    schema = _quoted_schema()
    op.add_column(_TABLE, sa.Column("target_geometry_resource_id", sa.String(length=80)))
    op.add_column(
        _TABLE,
        sa.Column("result_geometry_resource_version_id", sa.String(length=80)),
    )
    op.create_unique_constraint(
        "uq_geometry_resource_versions_resource_exact_version",
        "geometry_resource_versions",
        ("resource_id", "resource_version_id"),
    )
    op.create_foreign_key(
        "fk_critical_operations_exact_geometry_result_version",
        _TABLE,
        "geometry_resource_versions",
        ("target_geometry_resource_id", "result_geometry_resource_version_id"),
        ("resource_id", "resource_version_id"),
        ondelete="RESTRICT",
        use_alter=True,
    )
    for constraint in (_KIND_CHECK, _DETAIL_CHECK, _TERMINAL_CHECK, _ERROR_PHASE_CHECK):
        op.drop_constraint(op.f(constraint), _TABLE, type_="check")
    op.drop_index(_FINGERPRINT_INDEX, table_name=_TABLE)
    op.create_check_constraint(
        op.f(_KIND_CHECK),
        _TABLE,
        "operation_kind IN ('source_ingestion', 'data_transform', 'ifc_model_write', "
        "'geometry_resource_write')",
    )
    op.create_check_constraint(op.f(_DETAIL_CHECK), _TABLE, _detail_check(geometry=True))
    op.create_check_constraint(op.f(_TERMINAL_CHECK), _TABLE, _terminal_check(geometry=True))
    op.create_check_constraint(
        op.f(_ERROR_PHASE_CHECK),
        _TABLE,
        _error_phase_check(geometry=True),
    )
    op.create_index(
        _FINGERPRINT_INDEX,
        _TABLE,
        ("run_id", "tool_name", "request_fingerprint"),
        unique=True,
        postgresql_where=sa.text(
            "operation_kind IN ('source_ingestion', 'data_transform', 'geometry_resource_write')"
        ),
    )
    _create_authoring_guard()
    _create_geometry_rls_helpers(schema)
    op.execute(
        sa.text(
            "CREATE POLICY geometry_authoring_task_isolation ON critical_operations "
            "AS RESTRICTIVE USING (operation_kind <> 'geometry_resource_write' OR "
            "task_id = NULLIF(current_setting('cad2bim.task_id', true), '')) "
            "WITH CHECK (operation_kind <> 'geometry_resource_write' OR "
            "task_id = NULLIF(current_setting('cad2bim.task_id', true), ''))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY geometry_authoring_source_task_isolation ON artifacts "
            "AS RESTRICTIVE USING (kind <> 'geometry_authoring_source' OR "
            "task_id = NULLIF(current_setting('cad2bim.task_id', true), '')) "
            "WITH CHECK (kind <> 'geometry_authoring_source' OR "
            "task_id = NULLIF(current_setting('cad2bim.task_id', true), ''))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY geometry_authoring_source_version_task_isolation "
            "ON artifact_versions AS RESTRICTIVE "
            f"USING ({schema}.cad2bim_geometry_source_artifact_visible("
            "owner_id, task_id, artifact_id)) "
            f"WITH CHECK ({schema}.cad2bim_geometry_source_artifact_visible("
            "owner_id, task_id, artifact_id))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY geometry_authoring_catalog_task_isolation "
            "ON data_catalog_entries AS RESTRICTIVE "
            "USING (resource_kind <> 'operation' OR "
            f"{schema}.cad2bim_geometry_operation_catalog_visible("
            "owner_id, task_id, operation_id)) "
            "WITH CHECK (resource_kind <> 'operation' OR "
            f"{schema}.cad2bim_geometry_operation_catalog_visible("
            "owner_id, task_id, operation_id))"
        )
    )
    for role in _WRITE_ROLES:
        op.execute(
            sa.text(
                "GRANT UPDATE (result_geometry_resource_version_id) "
                f"ON TABLE critical_operations TO {role}"
            )
        )


def downgrade() -> None:
    schema = _quoted_schema()
    op.execute(
        sa.text(
            "LOCK TABLE critical_operations, artifacts, artifact_versions, "
            "data_catalog_entries IN ACCESS EXCLUSIVE MODE"
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                operation_count bigint;
                source_artifact_count bigint;
            BEGIN
                SELECT count(*) INTO operation_count
                  FROM critical_operations
                 WHERE operation_kind = 'geometry_resource_write'
                    OR target_geometry_resource_id IS NOT NULL
                    OR result_geometry_resource_version_id IS NOT NULL;
                SELECT count(*) INTO source_artifact_count
                  FROM artifacts
                 WHERE kind = 'geometry_authoring_source';
                IF operation_count <> 0 OR source_artifact_count <> 0 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = format(
                            'geometry authoring facts are not empty: '
                            'critical_operations=%s, source_artifacts=%s',
                            operation_count,
                            source_artifact_count
                        );
                END IF;
            END
            $$
            """
        )
    )
    for role in _WRITE_ROLES:
        op.execute(
            sa.text(
                "REVOKE UPDATE (result_geometry_resource_version_id) "
                f"ON TABLE critical_operations FROM {role}"
            )
        )
    op.execute(
        sa.text("DROP POLICY geometry_authoring_catalog_task_isolation ON data_catalog_entries")
    )
    op.execute(
        sa.text("DROP POLICY geometry_authoring_source_version_task_isolation ON artifact_versions")
    )
    op.execute(sa.text("DROP POLICY geometry_authoring_source_task_isolation ON artifacts"))
    op.execute(sa.text("DROP POLICY geometry_authoring_task_isolation ON critical_operations"))
    _drop_geometry_rls_helpers(schema)
    op.execute(
        sa.text("DROP TRIGGER trg_geometry_authoring_operation_guard ON critical_operations")
    )
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_geometry_authoring_operation()"))
    op.drop_index(_FINGERPRINT_INDEX, table_name=_TABLE)
    for constraint in (_KIND_CHECK, _DETAIL_CHECK, _TERMINAL_CHECK, _ERROR_PHASE_CHECK):
        op.drop_constraint(op.f(constraint), _TABLE, type_="check")
    op.create_check_constraint(
        op.f(_KIND_CHECK),
        _TABLE,
        "operation_kind IN ('source_ingestion', 'data_transform', 'ifc_model_write')",
    )
    op.create_check_constraint(op.f(_DETAIL_CHECK), _TABLE, _detail_check(geometry=False))
    op.create_check_constraint(op.f(_TERMINAL_CHECK), _TABLE, _terminal_check(geometry=False))
    op.create_check_constraint(
        op.f(_ERROR_PHASE_CHECK),
        _TABLE,
        _error_phase_check(geometry=False),
    )
    op.create_index(
        _FINGERPRINT_INDEX,
        _TABLE,
        ("run_id", "tool_name", "request_fingerprint"),
        unique=True,
        postgresql_where=sa.text("operation_kind IN ('source_ingestion', 'data_transform')"),
    )
    op.drop_constraint(
        "fk_critical_operations_exact_geometry_result_version",
        _TABLE,
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_geometry_resource_versions_resource_exact_version",
        "geometry_resource_versions",
        type_="unique",
    )
    op.drop_column(_TABLE, "result_geometry_resource_version_id")
    op.drop_column(_TABLE, "target_geometry_resource_id")
