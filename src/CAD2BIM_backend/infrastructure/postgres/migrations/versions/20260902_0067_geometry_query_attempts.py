"""Add Task-scoped durable geometry query attempts.

Revision ID: 20260902_0067
Revises: 20260902_0066
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260902_0067"
down_revision: str | None = "20260902_0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "geometry_query_attempts"
_READ_ROLES = (
    "cad2bim_api_runtime",
    "cad2bim_runner_runtime",
    "cad2bim_mcp_runtime",
)
_WRITE_ROLES = (
    "cad2bim_runner_runtime",
    "cad2bim_mcp_runtime",
)


def _lower_hex_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) = 64 AND {column_name} = lower({column_name}) "
        f"AND {remainder} = ''"
    )


def _create_guard_trigger() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_guard_geometry_query_attempt()
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
                        NEW.resource_version_id,
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
                        OLD.resource_version_id,
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

                IF NOT EXISTS (
                    SELECT 1
                      FROM geometry_resource_versions AS exact_version
                      JOIN geometry_resources AS resource
                        ON resource.resource_id = exact_version.resource_id
                     WHERE exact_version.resource_version_id = NEW.resource_version_id
                       AND (
                           resource.owner_scope = 'SYSTEM'
                           OR (
                               resource.owner_scope = 'TENANT'
                               AND resource.owner_tenant_id = NEW.tenant_id
                           )
                           OR (
                               resource.owner_scope = 'TASK'
                               AND resource.owner_tenant_id = NEW.tenant_id
                               AND resource.owner_task_id = NEW.task_id
                           )
                       )
                ) THEN
                    RAISE EXCEPTION 'geometry query resource version is outside the Task scope'
                        USING ERRCODE = '23503';
                END IF;

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
                       AND parent.resource_version_id = NEW.resource_version_id
                       AND parent.status IN ('SUCCEEDED', 'FAILED')
                ) THEN
                    RAISE EXCEPTION 'geometry query repair parent is outside the exact Task resource chain'
                        USING ERRCODE = '23503';
                END IF;

                IF jsonb_typeof(NEW.diagnostics_json) <> 'array'
                   OR jsonb_array_length(NEW.diagnostics_json) > 32
                   OR octet_length(NEW.diagnostics_json::text) > 16384
                   OR EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements(NEW.diagnostics_json) AS diagnostic
                        WHERE jsonb_typeof(diagnostic) <> 'object'
                           OR diagnostic - 'code' - 'message' <> '{}'::jsonb
                           OR jsonb_typeof(diagnostic -> 'code') <> 'string'
                           OR jsonb_typeof(diagnostic -> 'message') <> 'string'
                           OR diagnostic ->> 'code' !~ '^[A-Z][A-Z0-9_]{0,63}$'
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
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_geometry_query_attempt_guard "
            "BEFORE INSERT OR UPDATE ON geometry_query_attempts "
            "FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_geometry_query_attempt()"
        )
    )


def _enable_rls_and_grants() -> None:
    op.execute(sa.text(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY"))
    read_roles = ", ".join(_READ_ROLES)
    write_roles = ", ".join(_WRITE_ROLES)
    task_scope = (
        "tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '') AND "
        "task_id = NULLIF(current_setting('cad2bim.task_id', true), '')"
    )
    op.execute(
        sa.text(
            f"CREATE POLICY geometry_query_attempts_runtime_select ON {_TABLE} "
            f"FOR SELECT TO {read_roles} USING ({task_scope})"
        )
    )
    op.execute(
        sa.text(
            f"CREATE POLICY geometry_query_attempts_runtime_insert ON {_TABLE} "
            f"FOR INSERT TO {write_roles} WITH CHECK ({task_scope} AND status = 'PREPARED')"
        )
    )
    op.execute(
        sa.text(
            f"CREATE POLICY geometry_query_attempts_runtime_terminal ON {_TABLE} "
            f"FOR UPDATE TO {write_roles} USING ({task_scope}) "
            f"WITH CHECK ({task_scope} AND status IN ('SUCCEEDED', 'FAILED'))"
        )
    )
    for role in _READ_ROLES:
        op.execute(sa.text(f"GRANT SELECT ON TABLE {_TABLE} TO {role}"))
    for role in _WRITE_ROLES:
        op.execute(sa.text(f"GRANT INSERT ON TABLE {_TABLE} TO {role}"))
        op.execute(
            sa.text(
                "GRANT UPDATE (status, result_artifact_version_id, diagnostics_json, "
                f"finished_at) ON TABLE {_TABLE} TO {role}"
            )
        )


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("attempt_id", sa.String(length=80), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("origin_run_id", sa.String(length=80)),
        sa.Column("resource_version_id", sa.String(length=80), nullable=False),
        sa.Column("source_artifact_version_id", sa.String(length=80), nullable=False),
        sa.Column("result_artifact_version_id", sa.String(length=80)),
        sa.Column("repair_parent_attempt_id", sa.String(length=80)),
        sa.Column("request_identity", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "diagnostics_json",
            postgresql.JSONB(none_as_null=True),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_geometry_query_attempts_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_geometry_query_attempts_tenant_task_tasks",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["origin_run_id"],
            ["runs.run_id"],
            name="fk_geometry_query_attempts_origin_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resource_version_id"],
            ["geometry_resource_versions.resource_version_id"],
            name="fk_geometry_query_attempts_resource_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_version_id"],
            ["artifact_versions.version_id"],
            name="fk_geometry_query_attempts_source_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_artifact_version_id"],
            ["artifact_versions.version_id"],
            name="fk_geometry_query_attempts_result_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id", "repair_parent_attempt_id"],
            [
                "geometry_query_attempts.tenant_id",
                "geometry_query_attempts.task_id",
                "geometry_query_attempts.attempt_id",
            ],
            name="fk_geometry_query_attempts_repair_parent",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('PREPARED', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_geometry_query_attempts_status_valid"),
        ),
        sa.CheckConstraint(
            "request_identity <> '' AND request_identity = trim(request_identity)",
            name=op.f("ck_geometry_query_attempts_request_identity_valid"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("request_digest"),
            name=op.f("ck_geometry_query_attempts_request_digest_shape"),
        ),
        sa.CheckConstraint(
            "repair_parent_attempt_id IS NULL OR repair_parent_attempt_id <> attempt_id",
            name=op.f("ck_geometry_query_attempts_repair_parent_not_self"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(diagnostics_json) = 'array'",
            name=op.f("ck_geometry_query_attempts_diagnostics_array"),
        ),
        sa.CheckConstraint(
            "jsonb_array_length(diagnostics_json) <= 32",
            name=op.f("ck_geometry_query_attempts_diagnostics_count_bounded"),
        ),
        sa.CheckConstraint(
            "(status = 'PREPARED' AND result_artifact_version_id IS NULL "
            "AND diagnostics_json = '[]'::jsonb AND finished_at IS NULL) OR "
            "(status = 'SUCCEEDED' AND result_artifact_version_id IS NOT NULL "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'FAILED' AND result_artifact_version_id IS NULL "
            "AND jsonb_array_length(diagnostics_json) > 0 AND finished_at IS NOT NULL)",
            name=op.f("ck_geometry_query_attempts_terminal_shape_valid"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= created_at",
            name=op.f("ck_geometry_query_attempts_time_order_valid"),
        ),
        sa.PrimaryKeyConstraint("attempt_id", name=op.f("pk_geometry_query_attempts")),
        sa.UniqueConstraint(
            "tenant_id",
            "task_id",
            "attempt_id",
            name="uq_geometry_query_attempts_tenant_task_attempt",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "task_id",
            "request_identity",
            name="uq_geometry_query_attempts_task_request_identity",
        ),
    )
    op.create_index(
        "ix_geometry_query_attempts_tenant_id",
        _TABLE,
        ("tenant_id",),
    )
    op.create_index(
        "ix_geometry_query_attempts_task_created",
        _TABLE,
        ("tenant_id", "task_id", "created_at"),
    )
    op.create_index(
        "ix_geometry_query_attempts_resource_version_created",
        _TABLE,
        ("resource_version_id", "created_at"),
    )
    _create_guard_trigger()
    _enable_rls_and_grants()


def downgrade() -> None:
    op.execute(sa.text(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE"))
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                attempt_count bigint;
            BEGIN
                SELECT count(*) INTO attempt_count FROM geometry_query_attempts;
                IF attempt_count <> 0 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = format(
                            'geometry query attempt log is not empty: geometry_query_attempts=%s',
                            attempt_count
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
                "REVOKE UPDATE (status, result_artifact_version_id, diagnostics_json, "
                f"finished_at) ON TABLE {_TABLE} FROM {role}"
            )
        )
        op.execute(sa.text(f"REVOKE INSERT ON TABLE {_TABLE} FROM {role}"))
    for role in _READ_ROLES:
        op.execute(sa.text(f"REVOKE SELECT ON TABLE {_TABLE} FROM {role}"))
    op.execute(sa.text("DROP TRIGGER trg_geometry_query_attempt_guard ON geometry_query_attempts"))
    op.drop_table(_TABLE)
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_geometry_query_attempt()"))
