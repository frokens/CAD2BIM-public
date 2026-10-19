"""Add durable Run-scoped Data Query attempts and exact input bindings.

Revision ID: 20260904_0071
Revises: 20260903_0070
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql


revision: str = "20260904_0071"
down_revision: str | None = "20260903_0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ATTEMPTS = "data_query_attempts"
_INPUTS = "data_query_inputs"
_READ_ROLES = ("cad2bim_api_runtime", "cad2bim_runner_runtime", "cad2bim_mcp_runtime")
_WRITE_ROLES = ("cad2bim_runner_runtime", "cad2bim_mcp_runtime")


def _quoted_schema() -> str:
    if context.is_offline_mode():
        return "public"
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema() ")).scalar_one()
    return bind.dialect.identifier_preparer.quote(schema)


def _lower_hex(column: str) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"length({column}) = 64 AND {column} = lower({column}) AND {remainder} = ''"


def _create_attempts() -> None:
    op.create_table(
        _ATTEMPTS,
        sa.Column("attempt_id", sa.String(80), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("run_id", sa.String(80), nullable=False),
        sa.Column("engine", sa.String(32), nullable=False),
        sa.Column("source_artifact_id", sa.String(80), nullable=False),
        sa.Column("source_artifact_version_id", sa.String(80), nullable=False),
        sa.Column("repair_parent_attempt_id", sa.String(80)),
        sa.Column("request_identity", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("input_count", sa.Integer(), nullable=False),
        sa.Column("parameters_json", postgresql.JSONB(none_as_null=True), nullable=False),
        sa.Column("inline_tables_json", postgresql.JSONB(none_as_null=True), nullable=False),
        sa.Column("page_size", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("diagnostic_json", postgresql.JSONB(none_as_null=True)),
        sa.Column("result_summary_json", postgresql.JSONB(none_as_null=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_data_query_attempts_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_data_query_attempts_owner_task_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "source_artifact_id"],
            [
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ],
            name="fk_data_query_attempts_run_source_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "source_artifact_id", "source_artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_data_query_attempts_exact_source_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "repair_parent_attempt_id", "engine"],
            [
                "data_query_attempts.owner_id",
                "data_query_attempts.task_id",
                "data_query_attempts.run_id",
                "data_query_attempts.attempt_id",
                "data_query_attempts.engine",
            ],
            name="fk_data_query_attempts_repair_parent",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "engine IN ('duckdb_sql', 'python_arrow')",
            name=op.f("ck_data_query_attempts_engine_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('PREPARED', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_data_query_attempts_status_valid"),
        ),
        sa.CheckConstraint(
            "request_identity <> '' AND request_identity = btrim(request_identity)",
            name=op.f("ck_data_query_attempts_request_identity_valid"),
        ),
        sa.CheckConstraint(
            _lower_hex("request_digest"),
            name=op.f("ck_data_query_attempts_request_digest_shape"),
        ),
        sa.CheckConstraint(
            "input_count BETWEEN 1 AND 16",
            name=op.f("ck_data_query_attempts_input_count_valid"),
        ),
        sa.CheckConstraint(
            "sequence > 0",
            name=op.f("ck_data_query_attempts_sequence_positive"),
        ),
        sa.CheckConstraint(
            "page_size BETWEEN 1 AND 200",
            name=op.f("ck_data_query_attempts_page_size_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(parameters_json) = 'array' "
            "AND jsonb_array_length(parameters_json) <= 32 "
            "AND octet_length(parameters_json::text) <= 32768",
            name=op.f("ck_data_query_attempts_parameters_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(inline_tables_json) = 'array' "
            "AND jsonb_array_length(inline_tables_json) <= 8 "
            "AND octet_length(inline_tables_json::text) <= 1048576",
            name=op.f("ck_data_query_attempts_inline_tables_valid"),
        ),
        sa.CheckConstraint(
            "engine = 'python_arrow' OR inline_tables_json = '[]'::jsonb",
            name=op.f("ck_data_query_attempts_inline_tables_python_only"),
        ),
        sa.CheckConstraint(
            "repair_parent_attempt_id IS NULL OR repair_parent_attempt_id <> attempt_id",
            name=op.f("ck_data_query_attempts_repair_parent_not_self"),
        ),
        sa.CheckConstraint(
            "(status = 'PREPARED' AND diagnostic_json IS NULL AND result_summary_json IS NULL AND finished_at IS NULL) OR "
            "(status = 'SUCCEEDED' AND diagnostic_json IS NULL AND result_summary_json IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(status = 'FAILED' AND diagnostic_json IS NOT NULL AND result_summary_json IS NULL AND finished_at IS NOT NULL)",
            name=op.f("ck_data_query_attempts_terminal_shape_valid"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= created_at",
            name=op.f("ck_data_query_attempts_time_order_valid"),
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint(
            "owner_id", "task_id", "run_id", "attempt_id", name="uq_data_query_attempts_run_attempt"
        ),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "attempt_id",
            "engine",
            name="uq_data_query_attempts_run_attempt_engine",
        ),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "request_identity",
            name="uq_data_query_attempts_run_request_identity",
        ),
        sa.UniqueConstraint(
            "owner_id", "task_id", "run_id", "sequence", name="uq_data_query_attempts_run_sequence"
        ),
    )
    op.create_index("ix_data_query_attempts_tenant_id", _ATTEMPTS, ["tenant_id"])
    op.create_index(
        "ix_data_query_attempts_run_engine_created",
        _ATTEMPTS,
        ["owner_id", "task_id", "run_id", "engine", "created_at", "attempt_id"],
    )


def _create_inputs() -> None:
    op.create_table(
        _INPUTS,
        sa.Column("attempt_id", sa.String(80), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("run_id", sa.String(80), nullable=False),
        sa.Column("input_name", sa.String(63), nullable=False),
        sa.Column("package_id", sa.String(80), nullable=False),
        sa.Column("payload_member_id", sa.String(80), nullable=False),
        sa.Column("schema_member_id", sa.String(80)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_data_query_inputs_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "attempt_id"],
            [
                "data_query_attempts.owner_id",
                "data_query_attempts.task_id",
                "data_query_attempts.run_id",
                "data_query_attempts.attempt_id",
            ],
            name="fk_data_query_inputs_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "package_id", "payload_member_id"],
            [
                "data_package_members.owner_id",
                "data_package_members.task_id",
                "data_package_members.package_id",
                "data_package_members.member_id",
            ],
            name="fk_data_query_inputs_payload_member",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "package_id", "schema_member_id"],
            [
                "data_package_members.owner_id",
                "data_package_members.task_id",
                "data_package_members.package_id",
                "data_package_members.member_id",
            ],
            name="fk_data_query_inputs_schema_member",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "ordinal BETWEEN 1 AND 16",
            name=op.f("ck_data_query_inputs_ordinal_valid"),
        ),
        sa.CheckConstraint(
            "input_name ~ '^[a-z][a-z0-9_]{0,62}$'",
            name=op.f("ck_data_query_inputs_input_name_valid"),
        ),
        sa.PrimaryKeyConstraint("attempt_id", "ordinal"),
        sa.UniqueConstraint("attempt_id", "input_name", name="uq_data_query_inputs_attempt_name"),
        sa.UniqueConstraint(
            "attempt_id",
            "package_id",
            "payload_member_id",
            name="uq_data_query_inputs_attempt_payload",
        ),
    )
    op.create_index("ix_data_query_inputs_tenant_id", _INPUTS, ["tenant_id"])


def _create_artifact_visibility(schema: str) -> None:
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {schema}.cad2bim_data_query_artifact_visible(
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
                           artifact.kind <> 'data_query_source'
                           OR (
                               artifact.task_id = NULLIF(
                                   current_setting('cad2bim.task_id', true), ''
                               )
                               AND artifact.run_id = NULLIF(
                                   current_setting('cad2bim.run_id', true), ''
                               )
                           )
                       )
                )
            $function$
            """
        )
    )
    signature = "cad2bim_data_query_artifact_visible(text,text,text)"
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {schema}.{signature} FROM PUBLIC"))
    for role in _READ_ROLES:
        op.execute(sa.text(f"GRANT EXECUTE ON FUNCTION {schema}.{signature} TO {role}"))


def _create_source_artifact_guards() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_guard_data_query_source_artifact()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    IF OLD.kind = 'data_query_source' THEN
                        RAISE EXCEPTION 'Data Query source Artifacts are immutable'
                            USING ERRCODE = '23514';
                    END IF;
                    RETURN OLD;
                ELSIF TG_OP = 'INSERT' THEN
                    IF NEW.kind = 'data_query_source' THEN
                        IF pg_has_role(current_user, 'cad2bim_api_runtime', 'member')
                           AND NOT EXISTS (
                               SELECT 1 FROM pg_roles
                                WHERE rolname = current_user AND rolsuper
                           ) THEN
                            RAISE EXCEPTION 'API runtime cannot publish Data Query source Artifacts'
                                USING ERRCODE = '42501';
                        END IF;
                        IF NEW.run_id IS NULL OR NEW.status <> 'available'
                           OR NEW.current_version_id IS NOT NULL
                           OR NEW.content_type NOT IN ('application/sql', 'text/x-python') THEN
                            RAISE EXCEPTION 'Data Query source Artifact initial state is invalid'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;
                    RETURN NEW;
                END IF;

                IF OLD.kind <> 'data_query_source' AND NEW.kind <> 'data_query_source' THEN
                    RETURN NEW;
                END IF;
                IF pg_has_role(current_user, 'cad2bim_api_runtime', 'member')
                   AND NOT EXISTS (
                       SELECT 1 FROM pg_roles
                        WHERE rolname = current_user AND rolsuper
                   ) THEN
                    RAISE EXCEPTION 'API runtime cannot update Data Query source Artifacts'
                        USING ERRCODE = '42501';
                END IF;
                IF OLD.kind <> NEW.kind OR OLD.current_version_id IS NOT NULL
                   OR NEW.current_version_id IS NULL
                   OR ROW(
                       NEW.artifact_id, NEW.owner_id, NEW.tenant_id, NEW.task_id,
                       NEW.run_id, NEW.kind, NEW.filename, NEW.content_type,
                       NEW.status, NEW.idempotency_key, NEW.request_fingerprint,
                       NEW.created_at
                   ) IS DISTINCT FROM ROW(
                       OLD.artifact_id, OLD.owner_id, OLD.tenant_id, OLD.task_id,
                       OLD.run_id, OLD.kind, OLD.filename, OLD.content_type,
                       OLD.status, OLD.idempotency_key, OLD.request_fingerprint,
                       OLD.created_at
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM artifact_versions AS version
                        WHERE version.owner_id = NEW.owner_id
                          AND version.tenant_id = NEW.tenant_id
                          AND version.task_id = NEW.task_id
                          AND version.artifact_id = NEW.artifact_id
                          AND version.version_id = NEW.current_version_id
                          AND version.version_no = 1
                          AND version.content_type = NEW.content_type
                   ) THEN
                    RAISE EXCEPTION 'Data Query source Artifact publication is append-only'
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
            "CREATE TRIGGER trg_data_query_source_artifact_guard "
            "BEFORE INSERT OR UPDATE OR DELETE ON artifacts "
            "FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_data_query_source_artifact()"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_guard_data_query_source_artifact_version()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            DECLARE
                parent_kind text;
                parent_current_version_id text;
                parent_content_type text;
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    SELECT artifact.kind, artifact.current_version_id, artifact.content_type
                      INTO parent_kind, parent_current_version_id, parent_content_type
                      FROM artifacts AS artifact
                     WHERE artifact.owner_id = OLD.owner_id
                       AND artifact.task_id = OLD.task_id
                       AND artifact.artifact_id = OLD.artifact_id;
                ELSE
                    SELECT artifact.kind, artifact.current_version_id, artifact.content_type
                      INTO parent_kind, parent_current_version_id, parent_content_type
                      FROM artifacts AS artifact
                     WHERE artifact.owner_id = NEW.owner_id
                       AND artifact.task_id = NEW.task_id
                       AND artifact.artifact_id = NEW.artifact_id;
                END IF;
                IF NOT FOUND OR parent_kind <> 'data_query_source' THEN
                    IF TG_OP = 'DELETE' THEN
                        RETURN OLD;
                    END IF;
                    RETURN NEW;
                END IF;
                IF TG_OP <> 'INSERT' THEN
                    RAISE EXCEPTION 'Data Query source ArtifactVersions are immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF pg_has_role(current_user, 'cad2bim_api_runtime', 'member')
                   AND NOT EXISTS (
                       SELECT 1 FROM pg_roles
                        WHERE rolname = current_user AND rolsuper
                   ) THEN
                    RAISE EXCEPTION 'API runtime cannot publish Data Query source ArtifactVersions'
                        USING ERRCODE = '42501';
                END IF;
                IF NEW.version_no <> 1 OR parent_current_version_id IS NOT NULL
                   OR NEW.content_type <> parent_content_type THEN
                    RAISE EXCEPTION 'Data Query source ArtifactVersion publication is append-only'
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
            "CREATE TRIGGER trg_data_query_source_artifact_version_guard "
            "BEFORE INSERT OR UPDATE OR DELETE ON artifact_versions "
            "FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_data_query_source_artifact_version()"
        )
    )


def _create_guards() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_guard_data_query_attempt()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.status <> 'PREPARED' THEN
                        RAISE EXCEPTION 'Data Query attempt must be inserted prepared'
                            USING ERRCODE = '23514';
                    END IF;
                ELSE
                    IF OLD.status <> 'PREPARED'
                       OR NEW.status NOT IN ('SUCCEEDED', 'FAILED') THEN
                        RAISE EXCEPTION 'Data Query attempt terminal state is immutable'
                            USING ERRCODE = '23514';
                    END IF;
                    IF ROW(
                        NEW.attempt_id, NEW.sequence, NEW.owner_id, NEW.tenant_id,
                        NEW.task_id, NEW.run_id, NEW.engine, NEW.source_artifact_id,
                        NEW.source_artifact_version_id, NEW.repair_parent_attempt_id,
                        NEW.request_identity, NEW.request_digest, NEW.input_count,
                        NEW.parameters_json, NEW.inline_tables_json, NEW.page_size,
                        NEW.created_at
                    ) IS DISTINCT FROM ROW(
                        OLD.attempt_id, OLD.sequence, OLD.owner_id, OLD.tenant_id,
                        OLD.task_id, OLD.run_id, OLD.engine, OLD.source_artifact_id,
                        OLD.source_artifact_version_id, OLD.repair_parent_attempt_id,
                        OLD.request_identity, OLD.request_digest, OLD.input_count,
                        OLD.parameters_json, OLD.inline_tables_json, OLD.page_size,
                        OLD.created_at
                    ) THEN
                        RAISE EXCEPTION 'Data Query attempt identity is append-only'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;

                IF NOT EXISTS (
                    SELECT 1
                      FROM artifact_versions AS version
                      JOIN artifacts AS artifact
                        ON artifact.owner_id = version.owner_id
                       AND artifact.task_id = version.task_id
                       AND artifact.artifact_id = version.artifact_id
                     WHERE version.owner_id = NEW.owner_id
                       AND version.tenant_id = NEW.tenant_id
                       AND version.task_id = NEW.task_id
                       AND version.artifact_id = NEW.source_artifact_id
                       AND version.version_id = NEW.source_artifact_version_id
                       AND version.version_no = 1
                       AND artifact.run_id = NEW.run_id
                       AND artifact.kind = 'data_query_source'
                       AND artifact.status IN ('ready', 'available')
                       AND artifact.current_version_id = version.version_id
                       AND artifact.content_type = CASE NEW.engine
                           WHEN 'duckdb_sql' THEN 'application/sql'
                           WHEN 'python_arrow' THEN 'text/x-python'
                       END
                       AND version.content_type = artifact.content_type
                ) THEN
                    RAISE EXCEPTION 'Data Query exact source ArtifactVersion is invalid'
                        USING ERRCODE = '23503';
                END IF;

                IF NEW.repair_parent_attempt_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM data_query_attempts AS parent
                     WHERE parent.owner_id = NEW.owner_id
                       AND parent.tenant_id = NEW.tenant_id
                       AND parent.task_id = NEW.task_id
                       AND parent.run_id = NEW.run_id
                       AND parent.attempt_id = NEW.repair_parent_attempt_id
                       AND parent.engine = NEW.engine
                       AND parent.status IN ('SUCCEEDED', 'FAILED')
                       AND parent.parameters_json = NEW.parameters_json
                       AND parent.inline_tables_json = NEW.inline_tables_json
                ) THEN
                    RAISE EXCEPTION 'Data Query repair parent facts are invalid'
                        USING ERRCODE = '23503';
                END IF;

                IF NEW.diagnostic_json IS NOT NULL AND (
                    jsonb_typeof(NEW.diagnostic_json) <> 'object'
                    OR octet_length(NEW.diagnostic_json::text) > 4096
                ) THEN
                    RAISE EXCEPTION 'Data Query diagnostic is invalid'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.result_summary_json IS NOT NULL AND (
                    jsonb_typeof(NEW.result_summary_json) <> 'object'
                    OR octet_length(NEW.result_summary_json::text) > 8192
                ) THEN
                    RAISE EXCEPTION 'Data Query result summary is invalid'
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
            "CREATE TRIGGER trg_data_query_attempt_guard "
            "BEFORE INSERT OR UPDATE OR DELETE ON data_query_attempts "
            "FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_data_query_attempt()"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_guard_data_query_input()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            DECLARE
                payload_row data_package_members%ROWTYPE;
                schema_row data_package_members%ROWTYPE;
                attempt_engine text;
            BEGIN
                IF TG_OP <> 'INSERT' THEN
                    RAISE EXCEPTION 'Data Query exact inputs are immutable'
                        USING ERRCODE = '23514';
                END IF;

                SELECT attempt.engine INTO attempt_engine
                  FROM data_query_attempts AS attempt
                 WHERE attempt.owner_id = NEW.owner_id
                   AND attempt.tenant_id = NEW.tenant_id
                   AND attempt.task_id = NEW.task_id
                   AND attempt.run_id = NEW.run_id
                   AND attempt.attempt_id = NEW.attempt_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Data Query input lost its parent attempt'
                        USING ERRCODE = '23503';
                END IF;

                SELECT payload.* INTO payload_row
                  FROM data_package_members AS payload
                 WHERE payload.owner_id = NEW.owner_id
                   AND payload.tenant_id = NEW.tenant_id
                   AND payload.task_id = NEW.task_id
                   AND payload.package_id = NEW.package_id
                   AND payload.member_id = NEW.payload_member_id
                   AND payload.member_kind = 'payload';
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Data Query exact payload member is invalid'
                        USING ERRCODE = '23503';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM data_catalog_entries AS entry
                     WHERE entry.owner_id = NEW.owner_id
                       AND entry.tenant_id = NEW.tenant_id
                       AND entry.task_id = NEW.task_id
                       AND entry.run_id = NEW.run_id
                       AND entry.resource_kind = 'package'
                       AND entry.package_id = NEW.package_id
                ) THEN
                    RAISE EXCEPTION 'Data Query Package is not visible in this Run'
                        USING ERRCODE = '23503';
                END IF;

                IF payload_row.media_type = 'application/vnd.apache.parquet' THEN
                    IF NEW.schema_member_id IS NOT NULL THEN
                        RAISE EXCEPTION 'Parquet Data Query input cannot bind a schema member'
                            USING ERRCODE = '23514';
                    END IF;
                ELSIF payload_row.media_type = 'application/json'
                      AND attempt_engine = 'python_arrow' THEN
                    IF NEW.schema_member_id IS NULL THEN
                        RAISE EXCEPTION 'Python JSON input requires an exact schema member'
                            USING ERRCODE = '23514';
                    END IF;
                    SELECT schema_member.* INTO schema_row
                      FROM data_package_members AS schema_member
                     WHERE schema_member.owner_id = NEW.owner_id
                       AND schema_member.tenant_id = NEW.tenant_id
                       AND schema_member.task_id = NEW.task_id
                       AND schema_member.package_id = NEW.package_id
                       AND schema_member.member_id = NEW.schema_member_id
                       AND schema_member.member_kind = 'schema'
                       AND schema_member.media_type = 'application/schema+json'
                       AND schema_member.member_name =
                           'schemas/' || payload_row.logical_payload_name || '.schema.json';
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'Data Query exact JSON schema member is invalid'
                            USING ERRCODE = '23503';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'Data Query payload media type is unsupported by this engine'
                        USING ERRCODE = '23514';
                END IF;

                IF EXISTS (
                    SELECT 1 FROM data_query_attempts AS attempt
                     WHERE attempt.owner_id = NEW.owner_id
                       AND attempt.task_id = NEW.task_id
                       AND attempt.run_id = NEW.run_id
                       AND attempt.attempt_id = NEW.attempt_id
                       AND attempt.repair_parent_attempt_id IS NOT NULL
                ) AND NOT EXISTS (
                    SELECT 1
                      FROM data_query_attempts AS attempt
                      JOIN data_query_inputs AS parent_input
                        ON parent_input.owner_id = attempt.owner_id
                       AND parent_input.task_id = attempt.task_id
                       AND parent_input.run_id = attempt.run_id
                       AND parent_input.attempt_id = attempt.repair_parent_attempt_id
                     WHERE attempt.owner_id = NEW.owner_id
                       AND attempt.task_id = NEW.task_id
                       AND attempt.run_id = NEW.run_id
                       AND attempt.attempt_id = NEW.attempt_id
                       AND parent_input.ordinal = NEW.ordinal
                       AND parent_input.input_name = NEW.input_name
                       AND parent_input.package_id = NEW.package_id
                       AND parent_input.payload_member_id = NEW.payload_member_id
                       AND parent_input.schema_member_id
                           IS NOT DISTINCT FROM NEW.schema_member_id
                ) THEN
                    RAISE EXCEPTION 'Data Query repair must preserve the exact input set'
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
            "CREATE TRIGGER trg_data_query_input_guard "
            "BEFORE INSERT OR UPDATE OR DELETE ON data_query_inputs "
            "FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_data_query_input()"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_validate_data_query_input_set()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            DECLARE
                bound_count integer;
                minimum_ordinal integer;
                maximum_ordinal integer;
            BEGIN
                SELECT count(*), min(ordinal), max(ordinal)
                  INTO bound_count, minimum_ordinal, maximum_ordinal
                  FROM data_query_inputs
                 WHERE owner_id = NEW.owner_id
                   AND task_id = NEW.task_id
                   AND run_id = NEW.run_id
                   AND attempt_id = NEW.attempt_id;
                IF bound_count > 16 OR (
                    bound_count > 0 AND (
                        minimum_ordinal <> 1 OR maximum_ordinal <> bound_count
                    )
                ) THEN
                    RAISE EXCEPTION 'Data Query exact input ordinals are not contiguous'
                        USING ERRCODE = '23514';
                END IF;
                IF bound_count <> NEW.input_count THEN
                    RAISE EXCEPTION 'Data Query exact input count does not match child bindings'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.repair_parent_attempt_id IS NOT NULL AND (
                    EXISTS (
                        SELECT ordinal, input_name, package_id,
                               payload_member_id, schema_member_id
                          FROM data_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id AND attempt_id = NEW.attempt_id
                        EXCEPT
                        SELECT ordinal, input_name, package_id,
                               payload_member_id, schema_member_id
                          FROM data_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id
                           AND attempt_id = NEW.repair_parent_attempt_id
                    ) OR EXISTS (
                        SELECT ordinal, input_name, package_id,
                               payload_member_id, schema_member_id
                          FROM data_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id
                           AND attempt_id = NEW.repair_parent_attempt_id
                        EXCEPT
                        SELECT ordinal, input_name, package_id,
                               payload_member_id, schema_member_id
                          FROM data_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id AND attempt_id = NEW.attempt_id
                    )
                ) THEN
                    RAISE EXCEPTION 'Data Query repair must preserve the exact input set'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NULL;
            END
            $guard$
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE CONSTRAINT TRIGGER trg_data_query_input_set "
            "AFTER INSERT OR UPDATE ON data_query_attempts "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION cad2bim_validate_data_query_input_set()"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_validate_data_query_input_parent()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            DECLARE
                attempt data_query_attempts%ROWTYPE;
                bound_count integer;
            BEGIN
                SELECT * INTO attempt
                  FROM data_query_attempts AS candidate
                 WHERE candidate.owner_id = NEW.owner_id
                   AND candidate.task_id = NEW.task_id
                   AND candidate.run_id = NEW.run_id
                   AND candidate.attempt_id = NEW.attempt_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Data Query input lost its parent attempt'
                        USING ERRCODE = '23503';
                END IF;
                SELECT count(*) INTO bound_count
                  FROM data_query_inputs
                 WHERE owner_id = NEW.owner_id
                   AND task_id = NEW.task_id
                   AND run_id = NEW.run_id
                   AND attempt_id = NEW.attempt_id;
                IF bound_count <> attempt.input_count THEN
                    RAISE EXCEPTION 'Data Query exact input count does not match child bindings'
                        USING ERRCODE = '23514';
                END IF;
                IF attempt.repair_parent_attempt_id IS NOT NULL AND (
                    EXISTS (
                        SELECT ordinal, input_name, package_id,
                               payload_member_id, schema_member_id
                          FROM data_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id AND attempt_id = NEW.attempt_id
                        EXCEPT
                        SELECT ordinal, input_name, package_id,
                               payload_member_id, schema_member_id
                          FROM data_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id
                           AND attempt_id = attempt.repair_parent_attempt_id
                    ) OR EXISTS (
                        SELECT ordinal, input_name, package_id,
                               payload_member_id, schema_member_id
                          FROM data_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id
                           AND attempt_id = attempt.repair_parent_attempt_id
                        EXCEPT
                        SELECT ordinal, input_name, package_id,
                               payload_member_id, schema_member_id
                          FROM data_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id AND attempt_id = NEW.attempt_id
                    )
                ) THEN
                    RAISE EXCEPTION 'Data Query repair must preserve the exact input set'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NULL;
            END
            $guard$
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE CONSTRAINT TRIGGER trg_data_query_input_parent "
            "AFTER INSERT ON data_query_inputs "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION cad2bim_validate_data_query_input_parent()"
        )
    )


def _enable_rls_and_grants(schema: str) -> None:
    scope = (
        "tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '') AND "
        "task_id = NULLIF(current_setting('cad2bim.task_id', true), '') AND "
        "run_id = NULLIF(current_setting('cad2bim.run_id', true), '')"
    )
    read_roles = ", ".join(_READ_ROLES)
    write_roles = ", ".join(_WRITE_ROLES)
    for table in (_ATTEMPTS, _INPUTS):
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_runtime_select ON {table} FOR SELECT TO {read_roles} USING ({scope})"
            )
        )
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_runtime_insert ON {table} FOR INSERT TO {write_roles} WITH CHECK ({scope})"
            )
        )
        for role in _READ_ROLES:
            op.execute(sa.text(f"GRANT SELECT ON TABLE {table} TO {role}"))
        for role in _WRITE_ROLES:
            op.execute(sa.text(f"GRANT INSERT ON TABLE {table} TO {role}"))
    op.execute(
        sa.text(
            f"CREATE POLICY data_query_attempts_runtime_terminal ON {_ATTEMPTS} FOR UPDATE TO {write_roles} USING ({scope}) WITH CHECK ({scope} AND status IN ('SUCCEEDED','FAILED'))"
        )
    )
    for role in _WRITE_ROLES:
        op.execute(
            sa.text(
                f"GRANT UPDATE (status, diagnostic_json, result_summary_json, finished_at) ON TABLE {_ATTEMPTS} TO {role}"
            )
        )
    op.execute(
        sa.text(
            "CREATE POLICY data_query_source_artifact_run_isolation ON artifacts "
            "AS RESTRICTIVE USING (kind <> 'data_query_source' OR "
            "(task_id = NULLIF(current_setting('cad2bim.task_id', true), '') AND "
            "run_id = NULLIF(current_setting('cad2bim.run_id', true), ''))) "
            "WITH CHECK (kind <> 'data_query_source' OR "
            "(task_id = NULLIF(current_setting('cad2bim.task_id', true), '') AND "
            "run_id = NULLIF(current_setting('cad2bim.run_id', true), '')))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY data_query_source_artifact_version_run_isolation "
            "ON artifact_versions AS RESTRICTIVE "
            f"USING ({schema}.cad2bim_data_query_artifact_visible("
            "owner_id, task_id, artifact_id)) "
            f"WITH CHECK ({schema}.cad2bim_data_query_artifact_visible("
            "owner_id, task_id, artifact_id))"
        )
    )


def upgrade() -> None:
    schema = _quoted_schema()
    _create_attempts()
    _create_inputs()
    _create_artifact_visibility(schema)
    _create_source_artifact_guards()
    _create_guards()
    _enable_rls_and_grants(schema)


def downgrade() -> None:
    schema = _quoted_schema()
    op.execute(
        sa.text(
            "LOCK TABLE data_query_inputs, data_query_attempts, artifacts, "
            "artifact_versions IN ACCESS EXCLUSIVE MODE"
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                attempt_count bigint;
                input_count bigint;
                artifact_count bigint;
            BEGIN
                SELECT count(*) INTO attempt_count FROM data_query_attempts;
                SELECT count(*) INTO input_count FROM data_query_inputs;
                SELECT count(*) INTO artifact_count FROM artifacts
                 WHERE kind = 'data_query_source';
                IF attempt_count <> 0 OR input_count <> 0 OR artifact_count <> 0 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = format(
                            'Data Query history is not empty: attempts=%s, inputs=%s, artifacts=%s',
                            attempt_count, input_count, artifact_count
                        );
                END IF;
            END
            $$
            """
        )
    )
    op.execute(
        sa.text("DROP POLICY data_query_source_artifact_version_run_isolation ON artifact_versions")
    )
    op.execute(sa.text("DROP POLICY data_query_source_artifact_run_isolation ON artifacts"))
    op.execute(
        sa.text("DROP TRIGGER trg_data_query_source_artifact_version_guard ON artifact_versions")
    )
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_data_query_source_artifact_version()"))
    op.execute(sa.text("DROP TRIGGER trg_data_query_source_artifact_guard ON artifacts"))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_data_query_source_artifact()"))
    for table in (_INPUTS, _ATTEMPTS):
        for role in _WRITE_ROLES:
            op.execute(sa.text(f"REVOKE INSERT ON TABLE {table} FROM {role}"))
        for role in _READ_ROLES:
            op.execute(sa.text(f"REVOKE SELECT ON TABLE {table} FROM {role}"))
    for role in _WRITE_ROLES:
        op.execute(
            sa.text(
                "REVOKE UPDATE (status, diagnostic_json, result_summary_json, "
                f"finished_at) ON TABLE {_ATTEMPTS} FROM {role}"
            )
        )
    op.execute(sa.text("DROP TRIGGER trg_data_query_input_parent ON data_query_inputs"))
    op.execute(sa.text("DROP FUNCTION cad2bim_validate_data_query_input_parent()"))
    op.execute(sa.text("DROP TRIGGER trg_data_query_input_set ON data_query_attempts"))
    op.execute(sa.text("DROP FUNCTION cad2bim_validate_data_query_input_set()"))
    op.execute(sa.text("DROP TRIGGER trg_data_query_input_guard ON data_query_inputs"))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_data_query_input()"))
    op.execute(sa.text("DROP TRIGGER trg_data_query_attempt_guard ON data_query_attempts"))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_data_query_attempt()"))
    op.drop_table(_INPUTS)
    op.drop_table(_ATTEMPTS)
    signature = "cad2bim_data_query_artifact_visible(text,text,text)"
    for role in _READ_ROLES:
        op.execute(sa.text(f"REVOKE EXECUTE ON FUNCTION {schema}.{signature} FROM {role}"))
    op.execute(sa.text(f"DROP FUNCTION {schema}.{signature}"))
