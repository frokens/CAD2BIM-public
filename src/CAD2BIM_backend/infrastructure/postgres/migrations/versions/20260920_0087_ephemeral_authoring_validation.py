"""Keep authoring cases transient while preserving exact historical resources.

Revision ID: 20260920_0087
Revises: 20260920_0086
"""

from importlib import import_module

from alembic import op
import sqlalchemy as sa

revision = "20260920_0087"
down_revision = "20260920_0086"
branch_labels = None
depends_on = None


def _previous(name):
    return import_module("CAD2BIM_backend.infrastructure.postgres.migrations.versions." + name)


def _replace_function(name, old, new):
    # Retain the existing transaction, identity, scope and source guards.
    op.execute(sa.text(f"""
        DO $patch$
        DECLARE definition text;
        BEGIN
            SELECT pg_get_functiondef('{name}()'::regprocedure) INTO definition;
            IF position($before${old}$before$ IN definition) = 0 THEN
                RAISE EXCEPTION 'Expected authoring guard fragment is absent' USING ERRCODE = '23514';
            END IF;
            EXECUTE replace(definition, $before${old}$before$, $after${new}$after$);
        END $patch$
    """))


_VERSION_ADMISSION = """
            IF NEW.family_contract_json -> 'standard_model' IS DISTINCT FROM '{}'::jsonb
               OR NEW.validation_summary_json IS DISTINCT FROM '{"valid": true}'::jsonb THEN
                RAISE EXCEPTION 'New Family versions require formal construction and a minimal validation state'
                    USING ERRCODE = '23514';
            END IF;
"""
_OPERATION_ADMISSION = """
            IF NEW.operation_kind = 'product_family_write' AND (
                NEW.operation_detail_json #>> '{version_draft,draft_schema}' IS DISTINCT FROM 'product_family_authoring_draft_v2'
                OR (NEW.operation_detail_json -> 'version_draft') ? 'validation_scenarios'
                OR NEW.operation_detail_json #> '{version_draft,family_contract,standard_model}' IS DISTINCT FROM '{}'::jsonb
            ) THEN
                RAISE EXCEPTION 'New Family Operations cannot retain validation inputs' USING ERRCODE = '23514';
            END IF;
"""


def _current_admission(*, enable):
    for name, clause in (
        ("cad2bim_guard_product_family_current_version", _VERSION_ADMISSION),
        ("cad2bim_guard_product_family_current_operation", _OPERATION_ADMISSION),
    ):
        old, new = "RETURN NEW;", clause + "RETURN NEW;"
        _replace_function(name, old if enable else new, new if enable else old)


def _annotation(*, enable):
    previous = _previous("20260913_0085_version_usage_instructions")
    original = previous._GEOMETRY_ANNOTATION_USAGE_RESULT_GUARD
    formal = original.replace("- 'usage_instructions')", "- 'usage_instructions' - 'validation_profile' - 'runtime_manifest_sha256')")
    old_summary = """result_version.validation_summary_json =
                               (SELECT validation_summary_json
                                  FROM geometry_resource_versions
                                 WHERE resource_version_id =
                                      NEW.operation_detail_json ->> 'base_resource_version_id')"""
    new_summary = "result_version.validation_summary_json = '{\"valid\": true}'::jsonb"
    for old, new in ((original, formal), (old_summary, new_summary),
                     ("asset_role <> 'manifest'", "asset_role NOT IN ('manifest', 'validation_report')")):
        _replace_function("cad2bim_guard_geometry_authoring_operation", old if enable else new, new if enable else old)


def upgrade():
    _current_admission(enable=True)
    _replace_function("cad2bim_guard_product_family_version_complete",
                      "'standard_model_geometry', 'standard_model_manifest',\n                                   'validation_report')) <> 3",
                      "'standard_model_geometry', 'standard_model_manifest')) <> 2")
    _replace_function("cad2bim_guard_product_family_version_complete", "RETURN NEW;", """
        IF EXISTS (SELECT 1 FROM product_family_version_assets
                   WHERE family_version_id = NEW.family_version_id AND asset_role = 'validation_report') THEN
            RAISE EXCEPTION 'New Family versions cannot retain validation reports' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;""")
    _annotation(enable=True)
    op.execute(sa.text("""
        CREATE FUNCTION cad2bim_guard_geometry_formal_version()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        DECLARE kind text;
        BEGIN
            SELECT resource_kind INTO kind FROM geometry_resources WHERE resource_id = NEW.resource_id;
            IF NEW.validation_summary_json IS DISTINCT FROM '{"valid": true}'::jsonb
               OR NEW.manifest_json ?| ARRAY['validation_profile','validation_samples','samples','sample_executions']
               OR EXISTS (SELECT 1 FROM geometry_resource_version_assets
                          WHERE resource_version_id = NEW.resource_version_id AND asset_role = 'validation_report') THEN
                RAISE EXCEPTION 'New Geometry versions cannot retain validation cases or reports' USING ERRCODE = '23514';
            END IF;
            IF (kind = 'mapped_model' AND (SELECT count(*) FROM geometry_resource_version_assets
                    WHERE resource_version_id = NEW.resource_version_id AND asset_role IN ('canonical_geometry','manifest')) <> 2)
               OR (kind = 'modeling_function' AND (SELECT count(*) FROM geometry_resource_version_assets
                    WHERE resource_version_id = NEW.resource_version_id AND asset_role IN ('source_bundle','runtime_manifest','parameter_schema')) <> 3) THEN
                RAISE EXCEPTION 'Geometry formal assets are incomplete' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $guard$
    """))
    op.execute(sa.text("""
        CREATE CONSTRAINT TRIGGER trg_geometry_formal_version_guard
        AFTER INSERT ON geometry_resource_versions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_geometry_formal_version()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION cad2bim_guard_geometry_formal_source()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM artifacts WHERE artifact_id = NEW.artifact_id AND kind = 'geometry_authoring_source')
               AND (NEW.metadata_json ->> 'metadata_schema' IS DISTINCT FROM 'geometry_authoring_input_v2'
                    OR NEW.metadata_json - ARRAY['metadata_schema','resource_kind','geometry_inputs','parameter_schema'] <> '{}'::jsonb
                    OR (NEW.metadata_json ->> 'resource_kind' IN ('mapped_model','modeling_function')) IS NOT TRUE
                    OR (NEW.metadata_json ->> 'resource_kind' = 'modeling_function'
                        AND jsonb_typeof(NEW.metadata_json -> 'parameter_schema') IS DISTINCT FROM 'object')
                    OR (NEW.metadata_json ->> 'resource_kind' = 'mapped_model'
                        AND NEW.metadata_json -> 'parameter_schema' IS NOT NULL
                        AND NEW.metadata_json -> 'parameter_schema' <> 'null'::jsonb)) THEN
                RAISE EXCEPTION 'New Geometry source metadata requires formal inputs only' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $guard$
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_geometry_formal_source_guard BEFORE INSERT ON artifact_versions
        FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_geometry_formal_source()
    """))
    op.execute(sa.text("""
        CREATE FUNCTION cad2bim_guard_authoring_report_asset()
        RETURNS trigger LANGUAGE plpgsql AS $guard$
        BEGIN
            IF NEW.asset_role <> 'validation_report' THEN RETURN NEW; END IF;
            RAISE EXCEPTION 'New authoring assets cannot contain validation reports' USING ERRCODE = '23514';
        END $guard$
    """))
    for table in ("product_family_version_assets", "geometry_resource_version_assets"):
        op.execute(sa.text(f"CREATE TRIGGER trg_{table}_report_guard BEFORE INSERT ON {table} "
                           "FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_authoring_report_asset()"))


def downgrade():
    op.execute(sa.text("""
        DO $guard$ BEGIN
            IF EXISTS (SELECT 1 FROM critical_operations WHERE operation_detail_json #>> '{version_draft,draft_schema}' = 'product_family_authoring_draft_v2')
               OR EXISTS (SELECT 1 FROM artifact_versions WHERE metadata_json ->> 'metadata_schema' = 'geometry_authoring_input_v2')
               OR EXISTS (SELECT 1 FROM product_family_versions WHERE family_contract_json -> 'standard_model' = '{}'::jsonb)
               OR EXISTS (SELECT 1 FROM geometry_resource_versions v
                          WHERE v.validation_summary_json = '{"valid": true}'::jsonb) THEN
                RAISE EXCEPTION 'Cannot downgrade retained formal authoring facts' USING ERRCODE = '23514';
            END IF;
        END $guard$
    """))
    for table in ("product_family_version_assets", "geometry_resource_version_assets"):
        op.execute(sa.text(f"DROP TRIGGER trg_{table}_report_guard ON {table}"))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_authoring_report_asset()"))
    op.execute(sa.text("DROP TRIGGER trg_geometry_formal_source_guard ON artifact_versions"))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_geometry_formal_source()"))
    op.execute(sa.text("DROP TRIGGER trg_geometry_formal_version_guard ON geometry_resource_versions"))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_geometry_formal_version()"))
    _annotation(enable=False)
    _previous("20260909_0080_family_standard_model_assets")._guard(standard=True)
    _current_admission(enable=False)
