"""Remove authoring format generations; retain business identities and guards.

Revision ID: 20261001_0105
Revises: 20261001_0104

No saved JSON or asset bytes are relabelled. Historical development databases
are not runtime compatibility targets; required resources use the current DTO.
"""

from importlib import import_module
import re

from alembic import op
import sqlalchemy as sa

revision = "20261001_0105"
down_revision = "20261001_0104"
branch_labels = None
depends_on = None


def _previous(name):
    return import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions." + name)


def _remove_function_clauses(name, patterns):
    """Bounded migration of existing guards, never a runtime compatibility path."""
    connection = op.get_bind()
    definition = connection.execute(sa.text(
        "SELECT pg_get_functiondef(CAST(:function AS regprocedure))"
    ), {"function": name + "()"}).scalar_one()
    for pattern, replacement, expected in patterns:
        definition, count = re.subn(pattern, replacement, definition)
        if count != expected:
            raise RuntimeError(f"Expected {expected} obsolete clauses in {name}, found {count}")
    connection.exec_driver_sql(definition)


def upgrade():
    # Retain the complete Operation business constraint; remove only its ABI
    # enumeration. Draft, retained Preview and independent Publish stay intact.
    system = _previous("20260929_0097_system_family_publication")
    drafts = _previous("20260930_0100_authoring_drafts")
    publication = _previous("20261001_0101_independent_authoring_publication")
    detail = system._detail_check(system_copy=True)
    start = detail.index("AND (operation_detail_json #>> '{version_draft,abi_profile}'")
    end = detail.index("AND operation_detail_json ->> 'detail_kind'", start)
    detail = detail[:start] + detail[end:]
    detail = (f"({detail}) OR operation_kind='geometry_inspection' OR "
              f"COALESCE(({drafts._DRAFT_DETAIL}),FALSE) OR COALESCE(({publication._PUBLISH_DETAIL}),FALSE)")
    op.drop_constraint(op.f("ck_critical_operations_operation_kind_detail_valid"), "critical_operations", type_="check")
    op.execute(sa.text("ALTER TABLE critical_operations ADD CONSTRAINT "
        f"ck_critical_operations_operation_kind_detail_valid CHECK ({detail}) NOT VALID"))
    op.drop_constraint(op.f("ck_product_family_versions_contract_shape"), "product_family_versions", type_="check")
    op.create_check_constraint(op.f("ck_product_family_versions_contract_shape"), "product_family_versions",
        "jsonb_typeof(family_contract_json) = 'object' AND "
        "family_contract_json ->> 'product_structure_kind' IN ('single_product','semantic_assembly')")

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION cad2bim_guard_product_family_current_version()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF NEW.usage_instructions IS NULL
               OR NEW.validation_summary_json IS DISTINCT FROM '{"valid": true}'::jsonb THEN
                RAISE EXCEPTION 'Family publication requires usage instructions and successful construction'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $guard$;
        CREATE OR REPLACE FUNCTION cad2bim_guard_product_family_current_operation()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF NEW.operation_kind <> 'product_family_write'
               OR NEW.operation_detail_json ->> 'detail_kind' = 'authoring_draft_write' THEN
                RETURN NEW;
            END IF;
            IF jsonb_typeof(NEW.operation_detail_json #> '{version_draft,usage_instructions}')
                   IS DISTINCT FROM 'string'
               OR NOT (NEW.operation_detail_json #>> '{version_draft,usage_instructions}') ~ '[^[:space:]]'
               OR char_length(NEW.operation_detail_json #>> '{version_draft,usage_instructions}') > 8192
               OR octet_length(NEW.operation_detail_json #>> '{version_draft,usage_instructions}') > 32768
               OR (NEW.operation_detail_json -> 'version_draft') ? 'validation_scenarios' THEN
                RAISE EXCEPTION 'Family publication requires bounded usage and formal inputs'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $guard$;
    """))
    _remove_function_clauses("cad2bim_guard_product_family_authoring_operation", [
        (r"\s+AND (?:parent|NEW)\.operation_detail_json #>> '\{version_draft,(?:abi_profile|draft_schema|family_contract,schema)\}' =\s+'[^']+'", "", 6),
    ])
    _remove_function_clauses("cad2bim_guard_geometry_query_attempt", [
        (r" AND v\.abi_profile='product_realization_v5'\s+AND v\.family_contract_json->>'schema'='product_family_contract_v5'", "", 1),
        (r"\s+AND target_version\.metadata_json ->> 'format' = 'geometry_inspection_v1'", "", 1),
    ])
    _remove_function_clauses("cad2bim_guard_geometry_inspection_operation", [
        (r"\s+AND v\.metadata_json ->> 'format' = CASE WHEN NEW\.operation_detail_json->>'result_kind'='case_report' THEN 'geometry_preview_case_report_v1' ELSE 'geometry_inspection_v1' END", "", 1),
    ])
    _remove_function_clauses("cad2bim_guard_geometry_formal_source", [
        (r"NEW\.metadata_json ->> 'metadata_schema' IS DISTINCT FROM 'geometry_authoring_input_v2'\s+OR ", "", 1),
        (r"\['metadata_schema','resource_kind'", "['resource_kind'", 1),
    ])
    for field in ("abi_profile", "runtime_profile", "budget_profile"):
        op.drop_constraint(op.f(f"ck_product_family_versions_{field}_valid"), "product_family_versions", type_="check")
        op.drop_column("product_family_versions", field)


def downgrade():
    raise RuntimeError("Restore the pre-cleanup database snapshot; retired authoring contracts are not supported")
