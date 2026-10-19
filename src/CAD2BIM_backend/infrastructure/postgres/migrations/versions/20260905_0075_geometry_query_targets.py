"""Generalize geometry query attempts to exact closed target kinds.

Revision ID: 20260905_0075
Revises: 20260904_0074
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260905_0075"
down_revision: str | None = "20260904_0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "geometry_query_attempts"


def _install_guard(*, generalized: bool) -> None:
    target_identity = (
        """\
                        NEW.target_kind,
                        NEW.resource_version_id,
                        NEW.target_artifact_version_id,
                        NEW.target_geometry_format,"""
        if generalized
        else """\
                        NEW.resource_version_id,"""
    )
    old_target_identity = (
        """\
                        OLD.target_kind,
                        OLD.resource_version_id,
                        OLD.target_artifact_version_id,
                        OLD.target_geometry_format,"""
        if generalized
        else """\
                        OLD.resource_version_id,"""
    )
    target_validation = (
        """
                IF NEW.target_kind = 'geometry_resource_version' THEN
                    IF NOT EXISTS (
                        SELECT 1
                          FROM geometry_resource_versions AS exact_version
                          JOIN geometry_resources AS resource
                            ON resource.resource_id = exact_version.resource_id
                         WHERE exact_version.resource_version_id = NEW.resource_version_id
                           AND (
                               resource.owner_scope = 'SYSTEM'
                               OR (resource.owner_scope = 'TENANT'
                                   AND resource.owner_tenant_id = NEW.tenant_id)
                               OR (resource.owner_scope = 'TASK'
                                   AND resource.owner_tenant_id = NEW.tenant_id
                                   AND resource.owner_task_id = NEW.task_id)
                           )
                    ) THEN
                        RAISE EXCEPTION 'geometry query resource version is outside the Task scope'
                            USING ERRCODE = '23503';
                    END IF;
                ELSIF NEW.target_kind = 'product_realization_preview' THEN
                    IF NOT EXISTS (
                        SELECT 1
                          FROM artifact_versions AS target_version
                          JOIN artifacts AS target_artifact
                            ON target_artifact.artifact_id = target_version.artifact_id
                           AND target_artifact.owner_id = target_version.owner_id
                           AND target_artifact.task_id = target_version.task_id
                         WHERE target_version.version_id = NEW.target_artifact_version_id
                           AND target_version.tenant_id = NEW.tenant_id
                           AND target_version.task_id = NEW.task_id
                           AND target_artifact.run_id IS NOT DISTINCT FROM NEW.origin_run_id
                           AND target_artifact.kind = 'product_realization_preview_geometry'
                           AND target_version.content_type = 'application/vnd.opencascade.brep'
                           AND target_version.metadata_json ->> 'target_kind'
                               = 'product_realization_preview'
                           AND target_version.metadata_json ->> 'geometry_format'
                               = 'occt_brep_v1'
                           AND target_version.metadata_json ->> 'geometry_sha256'
                               = target_version.sha256
                           AND coalesce(target_version.metadata_json ->> 'family_version_id', '') <> ''
                           AND target_version.metadata_json ->> 'family_version_content_digest'
                               ~ '^[0-9a-f]{64}$'
                           AND target_version.metadata_json ->> 'solve_digest'
                               ~ '^[0-9a-f]{64}$'
                    ) THEN
                        RAISE EXCEPTION 'geometry query preview ArtifactVersion is outside the exact Task target'
                            USING ERRCODE = '23503';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'geometry query target kind is invalid'
                        USING ERRCODE = '23514';
                END IF;
    """
        if generalized
        else """
                IF NOT EXISTS (
                    SELECT 1
                      FROM geometry_resource_versions AS exact_version
                      JOIN geometry_resources AS resource
                        ON resource.resource_id = exact_version.resource_id
                     WHERE exact_version.resource_version_id = NEW.resource_version_id
                       AND (
                           resource.owner_scope = 'SYSTEM'
                           OR (resource.owner_scope = 'TENANT'
                               AND resource.owner_tenant_id = NEW.tenant_id)
                           OR (resource.owner_scope = 'TASK'
                               AND resource.owner_tenant_id = NEW.tenant_id
                               AND resource.owner_task_id = NEW.task_id)
                       )
                ) THEN
                    RAISE EXCEPTION 'geometry query resource version is outside the Task scope'
                        USING ERRCODE = '23503';
                END IF;
    """
    )
    parent_target = (
        """\
                       AND parent.target_kind = NEW.target_kind
                       AND parent.resource_version_id IS NOT DISTINCT FROM NEW.resource_version_id
                       AND parent.target_artifact_version_id
                           IS NOT DISTINCT FROM NEW.target_artifact_version_id
                       AND parent.target_geometry_format
                           IS NOT DISTINCT FROM NEW.target_geometry_format"""
        if generalized
        else """\
                       AND parent.resource_version_id = NEW.resource_version_id"""
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION cad2bim_guard_geometry_query_attempt()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    IF OLD.status <> 'PREPARED'
                       OR NEW.status NOT IN ('SUCCEEDED', 'FAILED') THEN
                        RAISE EXCEPTION 'geometry query attempt terminal state is immutable'
                            USING ERRCODE = '23514';
                    END IF;
                    IF ROW(
                        NEW.attempt_id,
                        NEW.tenant_id,
                        NEW.task_id,
                        NEW.origin_run_id,
{target_identity}
                        NEW.source_artifact_version_id,
                        NEW.repair_parent_attempt_id,
                        NEW.request_identity,
                        NEW.request_digest,
                        NEW.created_at
                    ) IS DISTINCT FROM ROW(
                        OLD.attempt_id,
                        OLD.tenant_id,
                        OLD.task_id,
                        OLD.origin_run_id,
{old_target_identity}
                        OLD.source_artifact_version_id,
                        OLD.repair_parent_attempt_id,
                        OLD.request_identity,
                        OLD.request_digest,
                        OLD.created_at
                    ) THEN
                        RAISE EXCEPTION 'geometry query attempt identity is append-only'
                            USING ERRCODE = '23514';
                    END IF;
                ELSIF NEW.status <> 'PREPARED' THEN
                    RAISE EXCEPTION 'geometry query attempt must be inserted prepared'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.origin_run_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM runs AS origin_run
                     WHERE origin_run.run_id = NEW.origin_run_id
                       AND origin_run.tenant_id = NEW.tenant_id
                       AND origin_run.task_id = NEW.task_id
                ) THEN
                    RAISE EXCEPTION 'geometry query origin Run is outside the Task'
                        USING ERRCODE = '23503';
                END IF;

{target_validation}

                IF NOT EXISTS (
                    SELECT 1 FROM artifact_versions AS source_version
                     WHERE source_version.version_id = NEW.source_artifact_version_id
                       AND source_version.tenant_id = NEW.tenant_id
                       AND source_version.task_id = NEW.task_id
                ) THEN
                    RAISE EXCEPTION 'geometry query source ArtifactVersion is outside the Task'
                        USING ERRCODE = '23503';
                END IF;

                IF NEW.result_artifact_version_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM artifact_versions AS result_version
                     WHERE result_version.version_id = NEW.result_artifact_version_id
                       AND result_version.tenant_id = NEW.tenant_id
                       AND result_version.task_id = NEW.task_id
                ) THEN
                    RAISE EXCEPTION 'geometry query result ArtifactVersion is outside the Task'
                        USING ERRCODE = '23503';
                END IF;

                IF NEW.repair_parent_attempt_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM geometry_query_attempts AS parent
                     WHERE parent.attempt_id = NEW.repair_parent_attempt_id
                       AND parent.tenant_id = NEW.tenant_id
                       AND parent.task_id = NEW.task_id
{parent_target}
                       AND parent.status IN ('SUCCEEDED', 'FAILED')
                ) THEN
                    RAISE EXCEPTION 'geometry query repair parent is outside the exact Task target chain'
                        USING ERRCODE = '23503';
                END IF;

                IF jsonb_typeof(NEW.diagnostics_json) <> 'array'
                   OR jsonb_array_length(NEW.diagnostics_json) > 32
                   OR octet_length(NEW.diagnostics_json::text) > 16384
                   OR EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements(NEW.diagnostics_json) AS diagnostic
                        WHERE jsonb_typeof(diagnostic) <> 'object'
                           OR diagnostic - 'code' - 'message' <> '{{}}'::jsonb
                           OR jsonb_typeof(diagnostic -> 'code') <> 'string'
                           OR jsonb_typeof(diagnostic -> 'message') <> 'string'
                           OR diagnostic ->> 'code' !~ '^[A-Z][A-Z0-9_]{{0,63}}$'
                           OR length(diagnostic ->> 'message') NOT BETWEEN 1 AND 2000
                           OR diagnostic ->> 'message' <> btrim(diagnostic ->> 'message')
                   ) THEN
                    RAISE EXCEPTION 'geometry query diagnostics exceed the bounded contract'
                        USING ERRCODE = '23514';
                END IF;

                RETURN NEW;
            END
            $guard$
            """
        )
    )


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "target_kind",
            sa.String(length=40),
            nullable=False,
            server_default=sa.text("'geometry_resource_version'"),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column("target_artifact_version_id", sa.String(length=80)),
    )
    op.add_column(
        _TABLE,
        sa.Column("target_geometry_format", sa.String(length=40)),
    )
    op.alter_column(_TABLE, "resource_version_id", nullable=True)
    op.create_foreign_key(
        "fk_geometry_query_attempts_target_artifact_version",
        _TABLE,
        "artifact_versions",
        ("target_artifact_version_id",),
        ("version_id",),
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_geometry_query_attempts_target_shape_valid"),
        _TABLE,
        "(target_kind = 'geometry_resource_version' "
        "AND resource_version_id IS NOT NULL "
        "AND target_artifact_version_id IS NULL "
        "AND target_geometry_format IS NULL) OR "
        "(target_kind = 'product_realization_preview' "
        "AND resource_version_id IS NULL "
        "AND target_artifact_version_id IS NOT NULL "
        "AND target_geometry_format = 'occt_brep_v1')",
    )
    op.create_index(
        "ix_geometry_query_attempts_target_artifact_created",
        _TABLE,
        ("target_artifact_version_id", "created_at"),
    )
    _install_guard(generalized=True)
    op.alter_column(_TABLE, "target_kind", server_default=None)


def downgrade() -> None:
    op.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE preview_count bigint;
            BEGIN
                SELECT count(*) INTO preview_count
                  FROM geometry_query_attempts
                 WHERE target_kind <> 'geometry_resource_version';
                IF preview_count <> 0 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = format(
                            'product realization preview query history exists: attempts=%s',
                            preview_count
                        );
                END IF;
            END
            $$
            """
        )
    )
    _install_guard(generalized=False)
    op.drop_index("ix_geometry_query_attempts_target_artifact_created", table_name=_TABLE)
    op.drop_constraint(
        op.f("ck_geometry_query_attempts_target_shape_valid"),
        _TABLE,
        type_="check",
    )
    op.drop_constraint(
        "fk_geometry_query_attempts_target_artifact_version",
        _TABLE,
        type_="foreignkey",
    )
    op.alter_column(_TABLE, "resource_version_id", nullable=False)
    op.drop_column(_TABLE, "target_geometry_format")
    op.drop_column(_TABLE, "target_artifact_version_id")
    op.drop_column(_TABLE, "target_kind")
