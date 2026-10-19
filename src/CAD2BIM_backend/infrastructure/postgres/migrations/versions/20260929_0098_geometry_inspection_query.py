"""Persist exact inspection choices on the existing Geometry Query owner.

Revision ID: 20260929_0098
Revises: 20260929_0097
"""

from importlib import import_module

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "20260929_0098"
down_revision = "20260929_0097"
branch_labels = None
depends_on = None

_TABLE = "geometry_query_attempts"
_TARGET_CHECK = "ck_geometry_query_attempts_target_shape_valid"
_CHOICE_CHECK = "ck_geometry_query_attempts_inspection_choices_valid"
_TARGET_SHAPE = """
    (target_kind = 'geometry_resource_version'
     AND resource_version_id IS NOT NULL AND target_artifact_version_id IS NULL
     AND target_geometry_format IS NULL AND inspection_selection_json IS NULL
     AND inspection_coordinate_space_json IS NULL)
    OR (target_kind = 'geometry_inspection'
     AND resource_version_id IS NULL AND target_artifact_version_id IS NOT NULL
     AND target_geometry_format IS NULL AND inspection_selection_json IS NOT NULL
     AND inspection_coordinate_space_json IS NOT NULL)
    OR (target_kind IN ('product_realization_preview', 'geometry_preview')
     AND resource_version_id IS NULL AND target_artifact_version_id IS NOT NULL
     AND target_geometry_format = 'occt_brep_v1' AND inspection_selection_json IS NULL
     AND inspection_coordinate_space_json IS NULL)
"""
_CHOICE_SHAPE = """
    target_kind <> 'geometry_inspection' OR (
      jsonb_typeof(inspection_selection_json) = 'object'
      AND ((inspection_selection_json ->> 'selection_kind' = 'whole'
            AND inspection_selection_json - 'selection_kind' = '{}'::jsonb)
        OR (inspection_selection_json ->> 'selection_kind' = 'component'
            AND inspection_selection_json ->> 'component_key' ~ '^[a-z][a-z0-9_]{0,63}$'
            AND inspection_selection_json - 'selection_kind' - 'component_key' = '{}'::jsonb)
        OR (inspection_selection_json ->> 'selection_kind' = 'fragment'
            AND inspection_selection_json ->> 'fragment_key' ~ '^[a-z][a-z0-9_]{0,63}$'
            AND inspection_selection_json - 'selection_kind' - 'fragment_key' = '{}'::jsonb))
      AND jsonb_typeof(inspection_coordinate_space_json) = 'object'
      AND ((inspection_coordinate_space_json ->> 'coordinate_space' = 'result'
            AND inspection_coordinate_space_json - 'coordinate_space' = '{}'::jsonb)
        OR (inspection_coordinate_space_json ->> 'coordinate_space' = 'fragment_local'
            AND inspection_coordinate_space_json ->> 'fragment_key' ~ '^[a-z][a-z0-9_]{0,63}$'
            AND inspection_coordinate_space_json - 'coordinate_space' - 'fragment_key' = '{}'::jsonb
            AND inspection_selection_json ->> 'selection_kind' = 'fragment'
            AND inspection_selection_json ->> 'fragment_key' = inspection_coordinate_space_json ->> 'fragment_key'))
    ) IS TRUE
"""


def _install_guard() -> None:
    previous = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260905_0075_geometry_query_targets"
    )
    previous._install_guard(generalized=True)
    op.execute(sa.text("""
        DO $migration$
        DECLARE
            definition text;
            target_start integer;
            source_start integer;
            parent_marker text := 'AND parent.target_geometry_format
                           IS NOT DISTINCT FROM NEW.target_geometry_format';
            inspection_branch text := $branch$
                ELSIF NEW.target_kind = 'geometry_inspection' THEN
                    IF NOT EXISTS (
                        SELECT 1 FROM artifact_versions AS target_version
                        JOIN artifacts AS target_artifact
                          ON target_artifact.artifact_id = target_version.artifact_id
                         AND target_artifact.owner_id = target_version.owner_id
                         AND target_artifact.task_id = target_version.task_id
                        WHERE target_version.version_id = NEW.target_artifact_version_id
                          AND target_version.tenant_id = NEW.tenant_id
                          AND target_version.task_id = NEW.task_id
                          AND target_artifact.kind = 'geometry_inspection'
                          AND target_version.content_type = 'application/vnd.cad2bim.geometry-inspection+zip'
                          AND target_version.metadata_json ->> 'format' = 'geometry_inspection_v1'
                          AND target_version.metadata_json ->> 'source_kind' IN ('geometry_sample', 'family_realization')
                          AND target_version.metadata_json ->> 'request_digest' ~ '^[0-9a-f]{64}$'
                    ) THEN
                        RAISE EXCEPTION 'geometry inspection query bundle is outside the exact Task target'
                            USING ERRCODE = '23503';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'new geometry queries cannot use retired preview targets'
                        USING ERRCODE = '23514';
                END IF;

                $branch$;
        BEGIN
            SELECT pg_get_functiondef('cad2bim_guard_geometry_query_attempt()'::regprocedure)
              INTO definition;
            target_start := strpos(definition, 'ELSIF NEW.target_kind = ''product_realization_preview'' THEN');
            source_start := strpos(definition, 'IF NOT EXISTS (
                    SELECT 1 FROM artifact_versions AS source_version');
            IF target_start = 0 OR source_start <= target_start
               OR strpos(definition, parent_marker) = 0 THEN
                RAISE EXCEPTION 'Geometry query predecessor guard markers changed';
            END IF;
            definition := overlay(definition placing inspection_branch
                from target_start for source_start - target_start);
            definition := replace(definition, 'NEW.target_geometry_format,',
                'NEW.target_geometry_format, NEW.inspection_selection_json, NEW.inspection_coordinate_space_json,');
            definition := replace(definition, 'OLD.target_geometry_format,',
                'OLD.target_geometry_format, OLD.inspection_selection_json, OLD.inspection_coordinate_space_json,');
            definition := replace(definition, parent_marker, parent_marker || '
                       AND parent.inspection_selection_json IS NOT DISTINCT FROM NEW.inspection_selection_json
                       AND parent.inspection_coordinate_space_json IS NOT DISTINCT FROM NEW.inspection_coordinate_space_json');
            definition := replace(definition,
                'diagnostic - ''code'' - ''message'' <> ''{}''::jsonb',
                'diagnostic - ''code'' - ''message'' - ''phase'' - ''retryable'' <> ''{}''::jsonb');
            definition := replace(definition,
                'OR jsonb_typeof(diagnostic -> ''message'') <> ''string''',
                'OR jsonb_typeof(diagnostic -> ''message'') <> ''string''
                           OR jsonb_typeof(diagnostic -> ''phase'') IS DISTINCT FROM ''string''
                           OR diagnostic ->> ''phase'' NOT IN (''read'', ''target'', ''preflight'', ''execution'', ''output_validation'')
                           OR jsonb_typeof(diagnostic -> ''retryable'') IS DISTINCT FROM ''boolean''');
            EXECUTE definition;
        END
        $migration$
    """))


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("inspection_selection_json", JSONB(), nullable=True))
    op.add_column(_TABLE, sa.Column("inspection_coordinate_space_json", JSONB(), nullable=True))
    op.drop_constraint(op.f(_TARGET_CHECK), _TABLE, type_="check")
    op.create_check_constraint(op.f(_TARGET_CHECK), _TABLE, _TARGET_SHAPE)
    op.create_check_constraint(op.f(_CHOICE_CHECK), _TABLE, _CHOICE_SHAPE)
    _install_guard()


def downgrade() -> None:
    op.execute(sa.text("LOCK TABLE geometry_query_attempts IN ACCESS EXCLUSIVE MODE"))
    op.execute(sa.text("""
        DO $guard$ BEGIN
            IF EXISTS (SELECT 1 FROM geometry_query_attempts WHERE target_kind = 'geometry_inspection') THEN
                RAISE EXCEPTION 'Cannot remove retained geometry inspection query history' USING ERRCODE = '23514';
            END IF;
        END $guard$
    """))
    previous = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260909_0082_family_self_geometry_contract"
    )
    previous._install_query_guard(self_geometry=True)
    op.drop_constraint(op.f(_CHOICE_CHECK), _TABLE, type_="check")
    op.drop_constraint(op.f(_TARGET_CHECK), _TABLE, type_="check")
    op.create_check_constraint(op.f(_TARGET_CHECK), _TABLE, previous._query_target_shape(self_geometry=True))
    op.drop_column(_TABLE, "inspection_coordinate_space_json")
    op.drop_column(_TABLE, "inspection_selection_json")
