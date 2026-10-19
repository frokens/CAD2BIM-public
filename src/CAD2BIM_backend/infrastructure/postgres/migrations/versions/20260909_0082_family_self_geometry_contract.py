"""Allow self-geometry Family contracts without rewriting immutable v1 facts.

Revision ID: 20260909_0082
Revises: 20260909_0081
"""

from importlib import import_module

from alembic import op
import sqlalchemy as sa


revision = "20260909_0082"
down_revision = "20260909_0081"
branch_labels = None
depends_on = None

_CONTRACT_CHECK = "ck_product_family_versions_contract_shape"
_OPERATION_CHECK = "ck_critical_operations_operation_kind_detail_valid"
_QUERY_CHECK = "ck_geometry_query_attempts_target_shape_valid"


def _query_target_shape(*, self_geometry: bool) -> str:
    preview_kind = (
        "target_kind IN ('product_realization_preview', 'geometry_preview')"
        if self_geometry else "target_kind = 'product_realization_preview'"
    )
    return (
        "(target_kind = 'geometry_resource_version' "
        "AND resource_version_id IS NOT NULL "
        "AND target_artifact_version_id IS NULL "
        "AND target_geometry_format IS NULL) OR (" + preview_kind + " "
        "AND resource_version_id IS NULL "
        "AND target_artifact_version_id IS NOT NULL "
        "AND target_geometry_format = 'occt_brep_v1')"
    )


def _install_query_guard(*, self_geometry: bool) -> None:
    previous = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260905_0075_geometry_query_targets"
    )
    previous._install_guard(generalized=True)
    if not self_geometry:
        return
    # Start from the immutable 0075 function, then add one separately scoped target.
    # The existing identity, repair, source/result and Family-target guards survive.
    op.execute(sa.text("""
        DO $migration$
        DECLARE
            old_definition text;
            marker text := 'ELSIF NEW.target_kind = ''product_realization_preview'' THEN';
            new_branch text := $branch$
                ELSIF NEW.target_kind = 'geometry_preview' THEN
                    IF NOT EXISTS (
                        SELECT 1 FROM artifact_versions AS target_version
                        JOIN artifacts AS target_artifact
                          ON target_artifact.artifact_id = target_version.artifact_id
                         AND target_artifact.owner_id = target_version.owner_id
                         AND target_artifact.task_id = target_version.task_id
                        WHERE target_version.version_id = NEW.target_artifact_version_id
                          AND target_version.tenant_id = NEW.tenant_id
                          AND target_version.task_id = NEW.task_id
                          AND target_artifact.run_id IS NOT DISTINCT FROM NEW.origin_run_id
                          AND target_artifact.kind = 'geometry_preview_sample_geometry'
                          AND target_version.content_type = 'application/vnd.opencascade.brep'
                          AND target_version.metadata_json ->> 'target_kind' = 'geometry_resource_preview'
                          AND target_version.metadata_json ->> 'geometry_format' = 'occt_brep_v1'
                          AND target_version.metadata_json ->> 'geometry_sha256' = target_version.sha256
                          AND coalesce(target_version.metadata_json ->> 'resource_id', '') <> ''
                          AND coalesce(target_version.metadata_json ->> 'resource_version_id', '') <> ''
                          AND target_version.metadata_json ->> 'resource_content_digest' ~ '^[0-9a-f]{64}$'
                          AND target_version.metadata_json -> 'derived_only' = 'true'::jsonb
                    ) THEN
                        RAISE EXCEPTION 'geometry query Geometry preview ArtifactVersion is outside the exact Task target'
                            USING ERRCODE = '23503';
                    END IF;
                $branch$;
        BEGIN
            SELECT pg_get_functiondef('cad2bim_guard_geometry_query_attempt()'::regprocedure)
              INTO old_definition;
            IF (length(old_definition) - length(replace(old_definition, marker, '')))
                / length(marker) <> 1 THEN
                RAISE EXCEPTION '0075 Geometry query target branch must occur exactly once';
            END IF;
            EXECUTE replace(old_definition, marker, new_branch || marker);
        END
        $migration$
    """))


def _contract_shape(*, self_geometry: bool) -> str:
    schema = (
        "family_contract_json ->> 'schema' "
        "IN ('product_family_contract_v1', 'product_family_contract_v2')"
        if self_geometry
        else "family_contract_json ->> 'schema' = 'product_family_contract_v1'"
    )
    return (
        "jsonb_typeof(family_contract_json) = 'object' AND "
        + schema
        + " AND family_contract_json ->> 'product_structure_kind' "
        "IN ('single_product', 'semantic_assembly')"
    )


def _operation_detail_check(*, self_geometry: bool) -> str:
    # Import only the immutable predecessor, never live models or Domain contracts.
    # This retains every A/0081 and historical Operation branch verbatim.
    previous = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260909_0081_ifc_products_apply"
    )._detail_check(products=True)
    if not self_geometry:
        return previous
    legacy_abi = (
        "operation_detail_json #>> '{version_draft,abi_profile}' = "
        "'product_realization_v1'"
    )
    modern_abi = (
        "(" + legacy_abi + " OR "
        "(operation_detail_json #>> '{version_draft,abi_profile}' = "
        "'product_realization_v2' AND (operation_detail_json #>> "
        "'{version_draft,family_contract,schema}' = 'product_family_contract_v2') IS TRUE))"
    )
    if previous.count(legacy_abi) != 1:
        raise RuntimeError("0081 Family ABI clause must occur exactly once")
    return previous.replace(legacy_abi, modern_abi, 1)


def _replace_checks(*, self_geometry: bool) -> None:
    op.drop_constraint(op.f(_CONTRACT_CHECK), "product_family_versions", type_="check")
    op.create_check_constraint(
        op.f(_CONTRACT_CHECK), "product_family_versions",
        _contract_shape(self_geometry=self_geometry),
    )
    op.drop_constraint(op.f(_OPERATION_CHECK), "critical_operations", type_="check")
    op.create_check_constraint(
        op.f(_OPERATION_CHECK), "critical_operations",
        _operation_detail_check(self_geometry=self_geometry),
    )
    op.drop_constraint(op.f(_QUERY_CHECK), "geometry_query_attempts", type_="check")
    op.create_check_constraint(
        op.f(_QUERY_CHECK), "geometry_query_attempts",
        _query_target_shape(self_geometry=self_geometry),
    )
    _install_query_guard(self_geometry=self_geometry)


def upgrade() -> None:
    _replace_checks(self_geometry=True)


def downgrade() -> None:
    op.execute(sa.text(
        "LOCK TABLE critical_operations, product_family_versions, geometry_query_attempts "
        "IN ACCESS EXCLUSIVE MODE"
    ))
    op.execute(sa.text("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM product_family_versions
                WHERE family_contract_json ->> 'schema' = 'product_family_contract_v2'
            ) OR EXISTS (
                SELECT 1 FROM critical_operations
                WHERE operation_kind = 'product_family_write'
                  AND operation_detail_json #>> '{version_draft,abi_profile}' =
                      'product_realization_v2'
            ) OR EXISTS (
                SELECT 1 FROM geometry_query_attempts WHERE target_kind = 'geometry_preview'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'Cannot downgrade while immutable self-geometry Family versions, Operations or Geometry preview queries exist';
            END IF;
        END $$
    """))
    _replace_checks(self_geometry=False)
