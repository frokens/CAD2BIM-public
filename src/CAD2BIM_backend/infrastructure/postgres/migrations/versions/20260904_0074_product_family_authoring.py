"""Add Operation-backed TASK ProductFamily authoring.

Revision ID: 20260904_0074
Revises: 20260904_0073
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

import rfc8785
import sqlalchemy as sa
from alembic import context, op


revision: str = "20260904_0074"
down_revision: str | None = "20260904_0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "critical_operations"
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
_FAMILY_TABLES = (
    "product_families",
    "product_family_versions",
    "product_family_geometry_bindings",
    "product_family_version_assets",
)
_PRODUCT_REALIZATION_ABI_PROFILE = "product_realization_v1"
_CONTENT_DIGEST_TABLES = (
    "product_family_versions",
    "product_family_geometry_bindings",
    "product_family_version_assets",
)


def _lower_slug_check(column_name: str) -> str:
    remainder = column_name
    for character in "abcdefghijklmnopqrstuvwxyz0123456789_":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) BETWEEN 1 AND 64 "
        f"AND substr({column_name}, 1, 1) BETWEEN 'a' AND 'z' "
        f"AND {remainder} = ''"
    )


def _family_content_digest(document: dict[str, Any]) -> str:
    """Migration-local RFC 8785 digest, independent of mutable domain helpers."""

    return hashlib.sha256(rfc8785.dumps(document)).hexdigest()


def _set_content_digest_tables_rls(*, enabled: bool) -> None:
    action = "ENABLE" if enabled else "DISABLE"
    for table in _CONTENT_DIGEST_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} {action} ROW LEVEL SECURITY"))
        if enabled:
            op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


def _rehash_product_family_versions(*, include_abi_profile: bool) -> None:
    if context.is_offline_mode():
        op.execute(
            sa.text(
                "DO $$ BEGIN IF EXISTS (SELECT 1 FROM product_family_versions) THEN "
                "RAISE EXCEPTION 'Nonempty ProductFamily ABI digest migration requires "
                "online Alembic execution' USING ERRCODE = '55000'; END IF; END $$"
            )
        )
        if include_abi_profile:
            op.execute(
                sa.text(
                    "UPDATE product_family_versions SET abi_profile = "
                    f"'{_PRODUCT_REALIZATION_ABI_PROFILE}'"
                )
            )
        return

    bind = op.get_bind()
    versions = bind.execute(
        sa.text(
            "SELECT family_version_id, contract_digest, program_entry, runtime_profile, "
            "budget_profile FROM product_family_versions ORDER BY family_version_id"
        )
    ).mappings().all()
    bindings_by_version: dict[str, list[dict[str, Any]]] = {}
    for row in bind.execute(
        sa.text(
            "SELECT family_version_id, binding_key, geometry_resource_id, "
            "geometry_resource_version_id, geometry_content_digest, "
            "expected_resource_kind, geometry_purpose "
            "FROM product_family_geometry_bindings "
            "ORDER BY family_version_id, binding_key"
        )
    ).mappings():
        bindings_by_version.setdefault(row["family_version_id"], []).append(
            {
                "binding_key": row["binding_key"],
                "geometry_resource_id": row["geometry_resource_id"],
                "geometry_resource_version_id": row["geometry_resource_version_id"],
                "geometry_content_digest": row["geometry_content_digest"],
                "expected_resource_kind": row["expected_resource_kind"],
                "geometry_purpose": row["geometry_purpose"],
            }
        )
    assets_by_version: dict[str, list[dict[str, Any]]] = {}
    for row in bind.execute(
        sa.text(
            "SELECT family_version_id, asset_role, asset_format, filename, content_type, "
            "size_bytes, sha256 FROM product_family_version_assets "
            "ORDER BY family_version_id, asset_role"
        )
    ).mappings():
        assets_by_version.setdefault(row["family_version_id"], []).append(
            {
                "asset_role": row["asset_role"],
                "asset_format": row["asset_format"],
                "filename": row["filename"],
                "content_type": row["content_type"],
                "size_bytes": row["size_bytes"],
                "sha256": row["sha256"],
            }
        )
    for row in versions:
        family_version_id = row["family_version_id"]
        document: dict[str, Any] = {
            "domain": "product_family_version_content",
            "contract_digest": row["contract_digest"],
            "program_entry": row["program_entry"],
            "runtime_profile": row["runtime_profile"],
            "budget_profile": row["budget_profile"],
            "geometry_bindings": bindings_by_version.get(family_version_id, []),
            "assets": assets_by_version.get(family_version_id, []),
        }
        if include_abi_profile:
            document["abi_profile"] = _PRODUCT_REALIZATION_ABI_PROFILE
        parameters = {
            "family_version_id": family_version_id,
            "content_digest": _family_content_digest(document),
        }
        if include_abi_profile:
            parameters["abi_profile"] = _PRODUCT_REALIZATION_ABI_PROFILE
        bind.execute(
            sa.text(
                "UPDATE product_family_versions SET content_digest = :content_digest"
                + (
                    ", abi_profile = :abi_profile"
                    if include_abi_profile
                    else ""
                )
                + " WHERE family_version_id = :family_version_id"
            ),
            parameters,
        )


def _quoted_schema() -> str:
    if context.is_offline_mode():
        return "public"
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema() ")).scalar_one()
    return bind.dialect.identifier_preparer.quote(schema)


def _legacy_detail_parts(*, family_columns: bool) -> tuple[str, str, str, str]:
    absent = (
        " AND target_product_family_id IS NULL "
        "AND base_product_family_version_id IS NULL "
        "AND result_product_family_version_id IS NULL"
        if family_columns
        else ""
    )
    source = (
        "(operation_kind = 'source_ingestion' AND tool_name = 'cad_import_source' "
        "AND source_filename IS NOT NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NULL AND code_artifact_version_id IS NULL "
        "AND code_artifact_sha256 IS NULL AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        "AND repair_parent_operation_id IS NULL "
        "AND base_ifc_revision_id IS NULL AND expected_ifc_write_epoch IS NULL "
        "AND target_geometry_resource_id IS NULL "
        f"AND result_geometry_resource_version_id IS NULL{absent})"
    )
    transform = (
        "(operation_kind = 'data_transform' "
        "AND tool_name IN ('data_transform', 'data_repair_transform', "
        "'cad_compute_measurements') AND source_filename IS NULL "
        "AND intent_summary IS NOT NULL AND code_artifact_id IS NOT NULL "
        "AND code_artifact_version_id IS NOT NULL AND code_artifact_sha256 IS NOT NULL "
        "AND ((tool_name = 'cad_compute_measurements' "
        "AND repair_parent_operation_id IS NULL) OR "
        "(tool_name = 'data_repair_transform' "
        "AND description_artifact_id IS NOT NULL "
        "AND description_artifact_version_id IS NOT NULL "
        "AND description_artifact_sha256 IS NOT NULL "
        "AND repair_parent_operation_id IS NOT NULL) OR "
        "(tool_name = 'data_transform' AND (repair_parent_operation_id IS NULL OR "
        "(repair_parent_operation_id IS NOT NULL "
        "AND description_artifact_id IS NOT NULL "
        "AND description_artifact_version_id IS NOT NULL "
        "AND description_artifact_sha256 IS NOT NULL)))) "
        "AND base_ifc_revision_id IS NULL AND expected_ifc_write_epoch IS NULL "
        "AND target_geometry_resource_id IS NULL "
        f"AND result_geometry_resource_version_id IS NULL{absent})"
    )
    ifc = (
        "(operation_kind = 'ifc_model_write' AND source_filename IS NULL "
        "AND intent_summary IS NOT NULL AND code_artifact_id IS NULL "
        "AND code_artifact_version_id IS NULL AND code_artifact_sha256 IS NULL "
        "AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        "AND repair_parent_operation_id IS NULL AND result_package_id IS NULL "
        "AND target_geometry_resource_id IS NULL "
        f"AND result_geometry_resource_version_id IS NULL{absent} "
        "AND ((tool_name = 'ifc_model_initialize' AND base_ifc_revision_id IS NULL "
        "AND expected_ifc_write_epoch IS NULL) OR "
        "(tool_name IN ('ifc_model_configure_geometry_basis', 'ifc_project_update', "
        "'ifc_spatial_structure_apply') AND base_ifc_revision_id IS NOT NULL "
        "AND expected_ifc_write_epoch IS NOT NULL)))"
    )
    geometry = (
        "(operation_kind = 'geometry_resource_write' "
        "AND tool_name IN ('ifc_geometry_resource_create', "
        "'ifc_geometry_resource_repair', 'ifc_geometry_resource_write') "
        "AND source_filename IS NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NOT NULL AND code_artifact_version_id IS NOT NULL "
        "AND code_artifact_sha256 IS NOT NULL AND description_artifact_id IS NULL "
        "AND description_artifact_version_id IS NULL "
        "AND description_artifact_sha256 IS NULL "
        "AND ((tool_name = 'ifc_geometry_resource_create' "
        "AND repair_parent_operation_id IS NULL) OR "
        "(tool_name = 'ifc_geometry_resource_repair' "
        "AND repair_parent_operation_id IS NOT NULL) OR "
        "tool_name = 'ifc_geometry_resource_write') "
        "AND result_package_id IS NULL AND base_ifc_revision_id IS NULL "
        "AND expected_ifc_write_epoch IS NULL "
        f"AND target_geometry_resource_id IS NOT NULL{absent})"
    )
    return source, transform, ifc, geometry


def _detail_check(*, product_family: bool) -> str:
    legacy = " OR ".join(_legacy_detail_parts(family_columns=product_family))
    if not product_family:
        return legacy
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
        f"'{_PRODUCT_REALIZATION_ABI_PROFILE}' "
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
    return f"{legacy} OR {family}"


def _terminal_check(*, product_family: bool) -> str:
    family_empty = " AND result_product_family_version_id IS NULL" if product_family else ""
    success_family = (
        " OR (operation_kind = 'product_family_write' "
        "AND result_package_id IS NULL "
        "AND result_geometry_resource_version_id IS NULL "
        "AND result_product_family_version_id IS NOT NULL)"
        if product_family
        else ""
    )
    return (
        "(status = 'prepared' AND finished_at IS NULL "
        "AND result_package_id IS NULL AND error_code IS NULL "
        "AND error_message IS NULL AND error_phase IS NULL "
        f"AND result_geometry_resource_version_id IS NULL{family_empty}) OR "
        "(status = 'succeeded' AND finished_at IS NOT NULL "
        "AND error_code IS NULL AND error_message IS NULL AND error_phase IS NULL "
        "AND ((operation_kind IN ('source_ingestion', 'data_transform') "
        "AND result_package_id IS NOT NULL "
        "AND result_geometry_resource_version_id IS NULL"
        f"{family_empty}) OR (operation_kind = 'ifc_model_write' "
        "AND result_package_id IS NULL "
        "AND result_geometry_resource_version_id IS NULL"
        f"{family_empty}) OR (operation_kind = 'geometry_resource_write' "
        "AND result_package_id IS NULL "
        "AND result_geometry_resource_version_id IS NOT NULL"
        f"{family_empty}){success_family})) OR "
        "(status = 'failed' AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL AND error_message IS NOT NULL "
        "AND result_package_id IS NULL "
        f"AND result_geometry_resource_version_id IS NULL{family_empty}) OR "
        "(status = 'indeterminate' AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL AND error_message IS NOT NULL "
        "AND error_phase = 'reconcile' AND result_package_id IS NULL "
        f"AND result_geometry_resource_version_id IS NULL{family_empty})"
    )


def _error_phase_check(*, product_family: bool) -> str:
    value = (
        "(status IN ('prepared', 'succeeded') AND error_phase IS NULL) OR "
        "(status = 'indeterminate' AND error_phase = 'reconcile') OR "
        "(status = 'failed' AND operation_kind = 'source_ingestion' "
        "AND (error_phase IS NULL OR error_phase = 'source_extraction')) OR "
        "(status = 'failed' AND operation_kind = 'data_transform' "
        "AND error_phase IN ('code_validation', 'execution', 'output_validation')) OR "
        "(status = 'failed' AND operation_kind = 'ifc_model_write' "
        "AND error_phase IN ('ifc_validation', 'storage_verification', 'commit_fence')) OR "
        "(status = 'failed' AND operation_kind = 'geometry_resource_write' "
        "AND error_phase IN ('code_validation', 'execution', 'output_validation', "
        "'storage_verification', 'commit_fence'))"
    )
    if not product_family:
        return value
    return (
        f"{value} OR (status = 'failed' AND operation_kind = 'product_family_write' "
        "AND error_phase IN ('code_validation', 'execution', 'output_validation', "
        "'storage_verification', 'commit_fence'))"
    )


def _create_rls_helpers(schema: str) -> None:
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {schema}.cad2bim_product_family_source_artifact_visible(
                p_owner_id text, p_task_id text, p_artifact_id text
            ) RETURNS boolean
            LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = {schema}, pg_temp SET row_security = off
            AS $function$
                SELECT EXISTS (
                    SELECT 1 FROM {schema}.artifacts AS artifact
                    WHERE artifact.owner_id = p_owner_id
                      AND artifact.task_id = p_task_id
                      AND artifact.artifact_id = p_artifact_id
                      AND artifact.tenant_id = NULLIF(
                          current_setting('cad2bim.tenant_id', true), '')
                      AND (artifact.kind <> 'product_family_authoring_source' OR
                           artifact.task_id = NULLIF(
                               current_setting('cad2bim.task_id', true), ''))
                )
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {schema}.cad2bim_product_family_operation_catalog_visible(
                p_owner_id text, p_task_id text, p_operation_id text
            ) RETURNS boolean
            LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = {schema}, pg_temp SET row_security = off
            AS $function$
                SELECT EXISTS (
                    SELECT 1 FROM {schema}.critical_operations AS operation
                    WHERE operation.owner_id = p_owner_id
                      AND operation.task_id = p_task_id
                      AND operation.operation_id = p_operation_id
                      AND operation.tenant_id = NULLIF(
                          current_setting('cad2bim.tenant_id', true), '')
                      AND (operation.operation_kind <> 'product_family_write' OR
                           operation.task_id = NULLIF(
                               current_setting('cad2bim.task_id', true), ''))
                )
            $function$
            """
        )
    )
    for function in (
        "cad2bim_product_family_source_artifact_visible(text,text,text)",
        "cad2bim_product_family_operation_catalog_visible(text,text,text)",
    ):
        op.execute(sa.text(f"REVOKE ALL ON FUNCTION {schema}.{function} FROM PUBLIC"))
        for role in _RLS_ROLES:
            op.execute(sa.text(f"GRANT EXECUTE ON FUNCTION {schema}.{function} TO {role}"))


def _create_operation_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_guard_product_family_authoring_operation()
            RETURNS trigger LANGUAGE plpgsql AS $guard$
            BEGIN
                IF NEW.operation_kind <> 'product_family_write' THEN
                    RETURN NEW;
                END IF;
                IF TG_OP = 'INSERT' THEN
                    IF NEW.status <> 'prepared' THEN
                        RAISE EXCEPTION 'ProductFamily Operation must be inserted prepared'
                            USING ERRCODE = '23514';
                    END IF;
                ELSE
                    IF NOT (
                        (OLD.status = 'prepared' AND
                         NEW.status IN ('succeeded', 'failed', 'indeterminate')) OR
                        (OLD.status = 'indeterminate' AND NEW.status = 'prepared')
                    ) THEN
                        RAISE EXCEPTION 'ProductFamily Operation terminal state is immutable'
                            USING ERRCODE = '23514';
                    END IF;
                    IF ROW(
                        NEW.operation_id, NEW.owner_id, NEW.tenant_id, NEW.task_id,
                        NEW.run_id, NEW.operation_kind, NEW.initiator, NEW.tool_name,
                        NEW.intent_summary, NEW.idempotency_key, NEW.request_fingerprint,
                        NEW.code_artifact_id, NEW.code_artifact_version_id,
                        NEW.code_artifact_sha256, NEW.repair_parent_operation_id,
                        NEW.target_product_family_id,
                        NEW.base_product_family_version_id,
                        NEW.operation_detail_json, NEW.started_at, NEW.created_at
                    ) IS DISTINCT FROM ROW(
                        OLD.operation_id, OLD.owner_id, OLD.tenant_id, OLD.task_id,
                        OLD.run_id, OLD.operation_kind, OLD.initiator, OLD.tool_name,
                        OLD.intent_summary, OLD.idempotency_key, OLD.request_fingerprint,
                        OLD.code_artifact_id, OLD.code_artifact_version_id,
                        OLD.code_artifact_sha256, OLD.repair_parent_operation_id,
                        OLD.target_product_family_id,
                        OLD.base_product_family_version_id,
                        OLD.operation_detail_json, OLD.started_at, OLD.created_at
                    ) THEN
                        RAISE EXCEPTION 'ProductFamily Operation identity is immutable'
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
                      AND source_artifact.kind = 'product_family_authoring_source'
                      AND source_artifact.status IN ('ready', 'available')
                ) THEN
                    RAISE EXCEPTION 'ProductFamily exact source ArtifactVersion is invalid'
                        USING ERRCODE = '23503';
                END IF;
                IF NEW.repair_parent_operation_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM critical_operations AS parent
                    WHERE parent.operation_id = NEW.repair_parent_operation_id
                      AND parent.owner_id = NEW.owner_id
                      AND parent.tenant_id = NEW.tenant_id
                      AND parent.task_id = NEW.task_id
                      AND parent.run_id = NEW.run_id
                      AND parent.operation_kind = 'product_family_write'
                      AND parent.status = 'failed'
                      AND parent.error_phase IN (
                          'code_validation', 'execution', 'output_validation'
                      )
                      AND parent.target_product_family_id = NEW.target_product_family_id
                      AND parent.base_product_family_version_id IS NOT DISTINCT FROM
                          NEW.base_product_family_version_id
                      AND parent.operation_detail_json -> 'version_draft' IS NOT NULL
                      AND NEW.operation_detail_json -> 'version_draft' IS NOT NULL
                      AND parent.operation_detail_json -> 'version_draft' =
                          NEW.operation_detail_json -> 'version_draft'
                      AND parent.code_artifact_id IS NOT NULL
                      AND parent.code_artifact_version_id IS NOT NULL
                      AND parent.code_artifact_sha256 IS NOT NULL
                ) THEN
                    RAISE EXCEPTION 'ProductFamily repair parent is not an exact failed chain parent'
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
            "CREATE TRIGGER trg_product_family_authoring_operation_guard "
            "BEFORE INSERT OR UPDATE ON critical_operations FOR EACH ROW "
            "EXECUTE FUNCTION cad2bim_guard_product_family_authoring_operation()"
        )
    )


def _create_family_insert_policies() -> None:
    roles = ", ".join(_WRITE_ROLES)
    root = (
        "owner_scope = 'TASK' AND owner_tenant_id = "
        "NULLIF(current_setting('cad2bim.tenant_id', true), '') AND owner_task_id = "
        "NULLIF(current_setting('cad2bim.task_id', true), '')"
    )
    children = {
        "product_family_versions": (
            "EXISTS (SELECT 1 FROM product_families AS family WHERE "
            "family.family_id = product_family_versions.family_id AND "
            f"{root.replace('owner_', 'family.owner_')})"
        ),
        "product_family_geometry_bindings": (
            "EXISTS (SELECT 1 FROM product_family_versions AS version "
            "JOIN product_families AS family ON family.family_id = version.family_id "
            "WHERE version.family_version_id = "
            "product_family_geometry_bindings.family_version_id AND "
            f"{root.replace('owner_', 'family.owner_')})"
        ),
        "product_family_version_assets": (
            "EXISTS (SELECT 1 FROM product_family_versions AS version "
            "JOIN product_families AS family ON family.family_id = version.family_id "
            "WHERE version.family_version_id = "
            "product_family_version_assets.family_version_id AND "
            f"{root.replace('owner_', 'family.owner_')})"
        ),
    }
    op.execute(
        sa.text(
            "CREATE POLICY product_families_runtime_insert ON product_families "
            f"FOR INSERT TO {roles} WITH CHECK ({root})"
        )
    )
    for table, condition in children.items():
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_runtime_insert ON {table} "
                f"FOR INSERT TO {roles} WITH CHECK ({condition})"
            )
        )
    tables = ", ".join(_FAMILY_TABLES)
    for role in _WRITE_ROLES:
        op.execute(sa.text(f"GRANT INSERT ON TABLE {tables} TO {role}"))


def upgrade() -> None:
    schema = _quoted_schema()
    op.add_column(
        "product_family_versions",
        sa.Column("abi_profile", sa.String(length=64), nullable=True),
    )
    _set_content_digest_tables_rls(enabled=False)
    _rehash_product_family_versions(include_abi_profile=True)
    # Rehashing a version schedules the existing deferred completeness trigger.
    # Flush it before ALTER TABLE changes RLS state in this transaction.
    op.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
    _set_content_digest_tables_rls(enabled=True)
    op.alter_column("product_family_versions", "abi_profile", nullable=False)
    op.create_check_constraint(
        op.f("ck_product_family_versions_abi_profile_valid"),
        "product_family_versions",
        _lower_slug_check("abi_profile"),
    )
    op.add_column(_TABLE, sa.Column("target_product_family_id", sa.String(length=80)))
    op.add_column(_TABLE, sa.Column("base_product_family_version_id", sa.String(length=80)))
    op.add_column(_TABLE, sa.Column("result_product_family_version_id", sa.String(length=80)))
    op.create_foreign_key(
        "fk_critical_operations_exact_product_family_base_version",
        _TABLE,
        "product_family_versions",
        ("target_product_family_id", "base_product_family_version_id"),
        ("family_id", "family_version_id"),
        ondelete="RESTRICT",
        use_alter=True,
    )
    op.create_foreign_key(
        "fk_critical_operations_exact_product_family_result_version",
        _TABLE,
        "product_family_versions",
        ("target_product_family_id", "result_product_family_version_id"),
        ("family_id", "family_version_id"),
        ondelete="RESTRICT",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_unique_constraint(
        "uq_critical_operations_result_product_family_version",
        _TABLE,
        ("result_product_family_version_id",),
    )
    for constraint in (_KIND_CHECK, _DETAIL_CHECK, _TERMINAL_CHECK, _ERROR_PHASE_CHECK):
        op.drop_constraint(op.f(constraint), _TABLE, type_="check")
    op.drop_index(_FINGERPRINT_INDEX, table_name=_TABLE)
    op.create_check_constraint(
        op.f(_KIND_CHECK),
        _TABLE,
        "operation_kind IN ('source_ingestion', 'data_transform', 'ifc_model_write', "
        "'geometry_resource_write', 'product_family_write')",
    )
    op.create_check_constraint(op.f(_DETAIL_CHECK), _TABLE, _detail_check(product_family=True))
    op.create_check_constraint(
        op.f(_TERMINAL_CHECK), _TABLE, _terminal_check(product_family=True)
    )
    op.create_check_constraint(
        op.f(_ERROR_PHASE_CHECK), _TABLE, _error_phase_check(product_family=True)
    )
    op.create_index(
        _FINGERPRINT_INDEX,
        _TABLE,
        ("run_id", "tool_name", "request_fingerprint"),
        unique=True,
        postgresql_where=sa.text(
            "operation_kind IN ('source_ingestion', 'data_transform', "
            "'geometry_resource_write', 'product_family_write')"
        ),
    )
    _create_operation_guard()
    _create_rls_helpers(schema)
    writer_membership = (
        "pg_has_role(session_user, 'cad2bim_mcp_runtime', 'member') OR "
        "pg_has_role(session_user, 'cad2bim_runner_runtime', 'member')"
    )
    op.execute(
        sa.text(
            "CREATE POLICY product_family_authoring_task_isolation ON critical_operations "
            "AS RESTRICTIVE USING (operation_kind <> 'product_family_write' OR "
            "task_id = NULLIF(current_setting('cad2bim.task_id', true), '')) "
            "WITH CHECK (operation_kind <> 'product_family_write' OR (("
            f"{writer_membership}) AND task_id = NULLIF("
            "current_setting('cad2bim.task_id', true), '')))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY product_family_source_task_isolation ON artifacts "
            "AS RESTRICTIVE USING (kind <> 'product_family_authoring_source' OR "
            "task_id = NULLIF(current_setting('cad2bim.task_id', true), '')) "
            "WITH CHECK (kind <> 'product_family_authoring_source' OR (("
            f"{writer_membership}) AND task_id = NULLIF("
            "current_setting('cad2bim.task_id', true), '')))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY product_family_source_version_task_isolation "
            "ON artifact_versions AS RESTRICTIVE USING ("
            f"{schema}.cad2bim_product_family_source_artifact_visible("
            "owner_id, task_id, artifact_id)) WITH CHECK ("
            f"{schema}.cad2bim_product_family_source_artifact_visible("
            "owner_id, task_id, artifact_id))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY product_family_catalog_task_isolation "
            "ON data_catalog_entries AS RESTRICTIVE "
            "USING (resource_kind <> 'operation' OR "
            f"{schema}.cad2bim_product_family_operation_catalog_visible("
            "owner_id, task_id, operation_id)) "
            "WITH CHECK (resource_kind <> 'operation' OR "
            f"{schema}.cad2bim_product_family_operation_catalog_visible("
            "owner_id, task_id, operation_id))"
        )
    )
    _create_family_insert_policies()
    for role in _WRITE_ROLES:
        op.execute(
            sa.text(
                "GRANT UPDATE (result_product_family_version_id) "
                f"ON TABLE critical_operations TO {role}"
            )
        )


def downgrade() -> None:
    schema = _quoted_schema()
    op.execute(
        sa.text(
            "LOCK TABLE critical_operations, artifacts, artifact_versions, "
            "data_catalog_entries, product_families, product_family_versions, "
            "product_family_geometry_bindings, product_family_version_assets "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE operation_count bigint; source_count bigint;
            BEGIN
                SELECT count(*) INTO operation_count FROM critical_operations
                WHERE operation_kind = 'product_family_write'
                   OR target_product_family_id IS NOT NULL
                   OR base_product_family_version_id IS NOT NULL
                   OR result_product_family_version_id IS NOT NULL;
                SELECT count(*) INTO source_count FROM artifacts
                WHERE kind = 'product_family_authoring_source';
                IF operation_count <> 0 OR source_count <> 0 THEN
                    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = format(
                        'ProductFamily authoring facts are not empty: operations=%s, sources=%s',
                        operation_count, source_count);
                END IF;
            END
            $$
            """
        )
    )
    op.drop_constraint(
        op.f("ck_product_family_versions_abi_profile_valid"),
        "product_family_versions",
        type_="check",
    )
    _set_content_digest_tables_rls(enabled=False)
    _rehash_product_family_versions(include_abi_profile=False)
    op.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
    _set_content_digest_tables_rls(enabled=True)
    op.drop_column("product_family_versions", "abi_profile")
    for role in _WRITE_ROLES:
        op.execute(
            sa.text(
                "REVOKE UPDATE (result_product_family_version_id) "
                f"ON TABLE critical_operations FROM {role}"
            )
        )
        op.execute(
            sa.text(
                "REVOKE INSERT ON TABLE "
                f"{', '.join(_FAMILY_TABLES)} FROM {role}"
            )
        )
    for table in reversed(_FAMILY_TABLES):
        op.execute(sa.text(f"DROP POLICY {table}_runtime_insert ON {table}"))
    op.execute(sa.text("DROP POLICY product_family_catalog_task_isolation ON data_catalog_entries"))
    op.execute(
        sa.text("DROP POLICY product_family_source_version_task_isolation ON artifact_versions")
    )
    op.execute(sa.text("DROP POLICY product_family_source_task_isolation ON artifacts"))
    op.execute(sa.text("DROP POLICY product_family_authoring_task_isolation ON critical_operations"))
    for function in (
        "cad2bim_product_family_operation_catalog_visible(text,text,text)",
        "cad2bim_product_family_source_artifact_visible(text,text,text)",
    ):
        for role in _RLS_ROLES:
            op.execute(sa.text(f"REVOKE EXECUTE ON FUNCTION {schema}.{function} FROM {role}"))
        op.execute(sa.text(f"DROP FUNCTION {schema}.{function}"))
    op.execute(
        sa.text("DROP TRIGGER trg_product_family_authoring_operation_guard ON critical_operations")
    )
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_product_family_authoring_operation()"))
    for constraint in (_KIND_CHECK, _DETAIL_CHECK, _TERMINAL_CHECK, _ERROR_PHASE_CHECK):
        op.drop_constraint(op.f(constraint), _TABLE, type_="check")
    op.drop_index(_FINGERPRINT_INDEX, table_name=_TABLE)
    op.drop_constraint(
        "uq_critical_operations_result_product_family_version", _TABLE, type_="unique"
    )
    op.drop_constraint(
        "fk_critical_operations_exact_product_family_result_version", _TABLE, type_="foreignkey"
    )
    op.drop_constraint(
        "fk_critical_operations_exact_product_family_base_version", _TABLE, type_="foreignkey"
    )
    op.drop_column(_TABLE, "result_product_family_version_id")
    op.drop_column(_TABLE, "base_product_family_version_id")
    op.drop_column(_TABLE, "target_product_family_id")
    op.create_check_constraint(
        op.f(_KIND_CHECK),
        _TABLE,
        "operation_kind IN ('source_ingestion', 'data_transform', 'ifc_model_write', "
        "'geometry_resource_write')",
    )
    op.create_check_constraint(op.f(_DETAIL_CHECK), _TABLE, _detail_check(product_family=False))
    op.create_check_constraint(
        op.f(_TERMINAL_CHECK), _TABLE, _terminal_check(product_family=False)
    )
    op.create_check_constraint(
        op.f(_ERROR_PHASE_CHECK), _TABLE, _error_phase_check(product_family=False)
    )
    op.create_index(
        _FINGERPRINT_INDEX,
        _TABLE,
        ("run_id", "tool_name", "request_fingerprint"),
        unique=True,
        postgresql_where=sa.text(
            "operation_kind IN ('source_ingestion', 'data_transform', "
            "'geometry_resource_write')"
        ),
    )
