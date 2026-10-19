"""Persist bounded Family and Geometry usage instructions without rewriting history.

Revision ID: 20260913_0085
Revises: 20260910_0084
"""

from alembic import op
import sqlalchemy as sa


revision = "20260913_0085"
down_revision = "20260910_0084"
branch_labels = None
depends_on = None


_GEOMETRY_ANNOTATION_USAGE_CHECK = (
    "ck_critical_operations_geometry_annotation_usage_current"
)
_GEOMETRY_ANNOTATION_OLD_RESULT_GUARD = """                           AND (result_version.manifest_json - 'interfaces') =
                               ((SELECT manifest_json FROM geometry_resource_versions
                                  WHERE resource_version_id =
                                      NEW.operation_detail_json ->> 'base_resource_version_id')
                                - 'interfaces')"""
_GEOMETRY_ANNOTATION_USAGE_RESULT_GUARD = """                           AND result_version.manifest_json -> 'usage_instructions' =
                                   NEW.operation_detail_json -> 'usage_instructions'
                           AND ((result_version.manifest_json - 'interfaces')
                                - 'usage_instructions') =
                               (((SELECT manifest_json FROM geometry_resource_versions
                                   WHERE resource_version_id =
                                      NEW.operation_detail_json ->> 'base_resource_version_id')
                                 - 'interfaces') - 'usage_instructions')"""


def _replace_geometry_annotation_result_guard(*, usage: bool) -> None:
    old = (
        _GEOMETRY_ANNOTATION_OLD_RESULT_GUARD
        if usage
        else _GEOMETRY_ANNOTATION_USAGE_RESULT_GUARD
    )
    new = (
        _GEOMETRY_ANNOTATION_USAGE_RESULT_GUARD
        if usage
        else _GEOMETRY_ANNOTATION_OLD_RESULT_GUARD
    )
    op.execute(sa.text(f"""
        DO $usage_guard$
        DECLARE
            current_definition text;
            patched_definition text;
        BEGIN
            SELECT pg_get_functiondef(
                'cad2bim_guard_geometry_authoring_operation()'::regprocedure
            ) INTO current_definition;
            IF current_definition IS NULL
               OR position($old_guard${old}$old_guard$ IN current_definition) = 0 THEN
                RAISE EXCEPTION 'Expected Geometry annotation result guard was not found'
                    USING ERRCODE = '23514';
            END IF;
            patched_definition := replace(
                current_definition,
                $old_guard${old}$old_guard$,
                $new_guard${new}$new_guard$
            );
            IF position($old_guard${old}$old_guard$ IN patched_definition) > 0 THEN
                RAISE EXCEPTION 'Geometry annotation result guard replacement was incomplete'
                    USING ERRCODE = '23514';
            END IF;
            EXECUTE patched_definition;
        END
        $usage_guard$
    """))


def upgrade() -> None:
    op.add_column(
        "product_family_versions",
        sa.Column("usage_instructions", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_product_family_versions_usage_instructions_bounded"),
        "product_family_versions",
        "usage_instructions IS NULL OR ("
        "usage_instructions ~ '[^[:space:]]' "
        "AND char_length(usage_instructions) <= 8192 "
        "AND octet_length(usage_instructions) <= 32768)",
    )
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION cad2bim_guard_product_family_current_version()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF NEW.family_contract_json ->> 'schema'
                    IS DISTINCT FROM 'product_family_contract_v2'
                OR NEW.abi_profile IS DISTINCT FROM 'product_realization_v2'
                OR NEW.usage_instructions IS NULL THEN
                RAISE EXCEPTION 'New ProductFamily versions require the current contract, ABI, and usage instructions'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $guard$
    """))
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION cad2bim_guard_product_family_current_operation()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF NEW.operation_kind = 'product_family_write' AND (
                NEW.operation_detail_json #>> '{version_draft,family_contract,schema}'
                    IS DISTINCT FROM 'product_family_contract_v2'
                OR NEW.operation_detail_json #>> '{version_draft,abi_profile}'
                    IS DISTINCT FROM 'product_realization_v2'
                OR jsonb_typeof(
                    NEW.operation_detail_json #> '{version_draft,usage_instructions}'
                ) IS DISTINCT FROM 'string'
                OR NOT (
                    NEW.operation_detail_json #>> '{version_draft,usage_instructions}'
                ) ~ '[^[:space:]]'
                OR char_length(
                    NEW.operation_detail_json #>> '{version_draft,usage_instructions}'
                ) > 8192
                OR octet_length(
                    NEW.operation_detail_json #>> '{version_draft,usage_instructions}'
                ) > 32768
            ) THEN
                RAISE EXCEPTION 'New ProductFamily Operations require the current contract, ABI, and usage instructions'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $guard$
    """))
    op.execute(sa.text(f"""
        ALTER TABLE critical_operations
        ADD CONSTRAINT {_GEOMETRY_ANNOTATION_USAGE_CHECK}
        CHECK (
            operation_kind IS DISTINCT FROM 'geometry_resource_write'
            OR operation_detail_json ->> 'detail_kind'
                IS DISTINCT FROM 'geometry_interface_annotation'
            OR (
                jsonb_typeof(operation_detail_json -> 'usage_instructions') = 'string'
                AND operation_detail_json ->> 'usage_instructions' ~ '[^[:space:]]'
                AND char_length(
                    operation_detail_json ->> 'usage_instructions'
                ) <= 8192
                AND octet_length(
                    operation_detail_json ->> 'usage_instructions'
                ) <= 32768
            )
        ) NOT VALID
    """))
    _replace_geometry_annotation_result_guard(usage=True)


def downgrade() -> None:
    op.execute(sa.text("""
        DO $guard$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM product_family_versions
                WHERE usage_instructions IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade while ProductFamily usage instructions exist; export or remove the authored versions first'
                    USING ERRCODE = '23514';
            END IF;
        END
        $guard$
    """))
    _replace_geometry_annotation_result_guard(usage=False)
    op.drop_constraint(
        op.f(_GEOMETRY_ANNOTATION_USAGE_CHECK),
        "critical_operations",
        type_="check",
    )
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION cad2bim_guard_product_family_current_operation()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF NEW.operation_kind = 'product_family_write' AND (
                NEW.operation_detail_json #>> '{version_draft,family_contract,schema}'
                    IS DISTINCT FROM 'product_family_contract_v2'
                OR NEW.operation_detail_json #>> '{version_draft,abi_profile}'
                    IS DISTINCT FROM 'product_realization_v2'
            ) THEN
                RAISE EXCEPTION 'New ProductFamily Operations require the current contract and ABI'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $guard$
    """))
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION cad2bim_guard_product_family_current_version()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF NEW.family_contract_json ->> 'schema'
                    IS DISTINCT FROM 'product_family_contract_v2'
                OR NEW.abi_profile IS DISTINCT FROM 'product_realization_v2' THEN
                RAISE EXCEPTION 'New ProductFamily versions require the current contract and ABI'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $guard$
    """))
    op.drop_constraint(
        op.f("ck_product_family_versions_usage_instructions_bounded"),
        "product_family_versions",
        type_="check",
    )
    op.drop_column("product_family_versions", "usage_instructions")
