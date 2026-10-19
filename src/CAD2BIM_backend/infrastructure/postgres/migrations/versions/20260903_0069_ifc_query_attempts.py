"""Add durable exact IFC query attempts and Package input bindings.

Revision ID: 20260903_0069
Revises: 20260902_0068
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql


revision: str = "20260903_0069"
down_revision: str | None = "20260902_0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ATTEMPTS = "ifc_query_attempts"
_INPUTS = "ifc_query_inputs"
_READ_ROLES = (
    "cad2bim_api_runtime",
    "cad2bim_runner_runtime",
    "cad2bim_mcp_runtime",
)
_WRITE_ROLES = (
    "cad2bim_runner_runtime",
    "cad2bim_mcp_runtime",
)


def _quoted_schema() -> str:
    if context.is_offline_mode():
        return "public"
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema() ")).scalar_one()
    return bind.dialect.identifier_preparer.quote(schema)


def _lower_hex_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) = 64 AND {column_name} = lower({column_name}) AND {remainder} = ''"
    )


def _create_artifact_visibility(schema: str) -> None:
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {schema}.cad2bim_ifc_query_artifact_visible(
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
                           artifact.kind NOT IN ('ifc_query_source', 'ifc_query_result')
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
    signature = "cad2bim_ifc_query_artifact_visible(text,text,text)"
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {schema}.{signature} FROM PUBLIC"))
    for role in _READ_ROLES:
        op.execute(sa.text(f"GRANT EXECUTE ON FUNCTION {schema}.{signature} TO {role}"))


def _create_query_artifact_guards() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_guard_ifc_query_artifact()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.kind IN ('ifc_query_source', 'ifc_query_result') THEN
                        IF pg_has_role(current_user, 'cad2bim_api_runtime', 'member')
                           AND NOT EXISTS (
                               SELECT 1 FROM pg_roles
                                WHERE rolname = current_user AND rolsuper
                           ) THEN
                            RAISE EXCEPTION 'API runtime cannot publish IFC query Artifacts'
                                USING ERRCODE = '42501';
                        END IF;
                        IF NEW.run_id IS NULL OR NEW.status <> 'available'
                           OR NEW.current_version_id IS NOT NULL THEN
                            RAISE EXCEPTION 'IFC query Artifact initial state is invalid'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;
                    RETURN NEW;
                END IF;

                IF TG_OP = 'DELETE' THEN
                    IF OLD.kind IN ('ifc_query_source', 'ifc_query_result') THEN
                        RAISE EXCEPTION 'IFC query Artifacts are immutable'
                            USING ERRCODE = '23514';
                    END IF;
                    RETURN OLD;
                END IF;

                IF OLD.kind NOT IN ('ifc_query_source', 'ifc_query_result')
                   AND NEW.kind NOT IN ('ifc_query_source', 'ifc_query_result') THEN
                    RETURN NEW;
                END IF;
                IF pg_has_role(current_user, 'cad2bim_api_runtime', 'member')
                   AND NOT EXISTS (
                       SELECT 1 FROM pg_roles
                        WHERE rolname = current_user AND rolsuper
                   ) THEN
                    RAISE EXCEPTION 'API runtime cannot update IFC query Artifacts'
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
                   ) THEN
                    RAISE EXCEPTION 'IFC query Artifact publication is append-only'
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
            "CREATE TRIGGER trg_ifc_query_artifact_guard "
            "BEFORE INSERT OR UPDATE OR DELETE ON artifacts "
            "FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_ifc_query_artifact()"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_guard_ifc_query_artifact_version()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            DECLARE
                parent_kind text;
                parent_current_version_id text;
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    SELECT artifact.kind, artifact.current_version_id
                      INTO parent_kind, parent_current_version_id
                      FROM artifacts AS artifact
                     WHERE artifact.owner_id = OLD.owner_id
                       AND artifact.task_id = OLD.task_id
                       AND artifact.artifact_id = OLD.artifact_id;
                ELSE
                    SELECT artifact.kind, artifact.current_version_id
                      INTO parent_kind, parent_current_version_id
                      FROM artifacts AS artifact
                     WHERE artifact.owner_id = NEW.owner_id
                       AND artifact.task_id = NEW.task_id
                       AND artifact.artifact_id = NEW.artifact_id;
                END IF;
                IF NOT FOUND OR parent_kind NOT IN ('ifc_query_source', 'ifc_query_result') THEN
                    IF TG_OP = 'DELETE' THEN
                        RETURN OLD;
                    END IF;
                    RETURN NEW;
                END IF;
                IF TG_OP <> 'INSERT' THEN
                    RAISE EXCEPTION 'IFC query ArtifactVersions are immutable'
                        USING ERRCODE = '23514';
                END IF;
                IF pg_has_role(current_user, 'cad2bim_api_runtime', 'member')
                   AND NOT EXISTS (
                       SELECT 1 FROM pg_roles
                        WHERE rolname = current_user AND rolsuper
                   ) THEN
                    RAISE EXCEPTION 'API runtime cannot publish IFC query ArtifactVersions'
                        USING ERRCODE = '42501';
                END IF;
                IF NEW.version_no <> 1 OR parent_current_version_id IS NOT NULL THEN
                    RAISE EXCEPTION 'IFC query ArtifactVersion publication is append-only'
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
            "CREATE TRIGGER trg_ifc_query_artifact_version_guard "
            "BEFORE INSERT OR UPDATE OR DELETE ON artifact_versions "
            "FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_ifc_query_artifact_version()"
        )
    )


def _create_attempt_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_guard_ifc_query_attempt()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.status <> 'PREPARED' THEN
                        RAISE EXCEPTION 'IFC query attempt must be inserted prepared'
                            USING ERRCODE = '23514';
                    END IF;
                ELSE
                    IF OLD.status <> 'PREPARED'
                       OR NEW.status NOT IN ('SUCCEEDED', 'FAILED') THEN
                        RAISE EXCEPTION 'IFC query attempt terminal state is immutable'
                            USING ERRCODE = '23514';
                    END IF;
                    IF ROW(
                        NEW.attempt_id, NEW.owner_id, NEW.tenant_id, NEW.task_id,
                        NEW.run_id, NEW.ifc_model_id, NEW.ifc_revision_id,
                        NEW.source_artifact_id, NEW.source_artifact_version_id,
                        NEW.repair_parent_attempt_id, NEW.request_identity,
                        NEW.request_digest, NEW.input_count, NEW.budget_json, NEW.created_at
                    ) IS DISTINCT FROM ROW(
                        OLD.attempt_id, OLD.owner_id, OLD.tenant_id, OLD.task_id,
                        OLD.run_id, OLD.ifc_model_id, OLD.ifc_revision_id,
                        OLD.source_artifact_id, OLD.source_artifact_version_id,
                        OLD.repair_parent_attempt_id, OLD.request_identity,
                        OLD.request_digest, OLD.input_count, OLD.budget_json, OLD.created_at
                    ) THEN
                        RAISE EXCEPTION 'IFC query attempt identity is append-only'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;

                IF jsonb_typeof(NEW.budget_json) <> 'object'
                   OR NOT (NEW.budget_json ? 'max_result_bytes')
                   OR NEW.budget_json - 'max_result_bytes' <> '{}'::jsonb
                   OR jsonb_typeof(NEW.budget_json -> 'max_result_bytes') <> 'number'
                   OR (NEW.budget_json ->> 'max_result_bytes')::bigint NOT BETWEEN 1 AND 1048576
                THEN
                    RAISE EXCEPTION 'IFC query budget summary is invalid'
                        USING ERRCODE = '23514';
                END IF;

                IF jsonb_typeof(NEW.diagnostics_json) <> 'array'
                   OR jsonb_array_length(NEW.diagnostics_json) > 32
                   OR octet_length(NEW.diagnostics_json::text) > 32768
                   OR EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements(NEW.diagnostics_json) AS diagnostic
                        WHERE jsonb_typeof(diagnostic) <> 'object'
                           OR NOT (diagnostic ?& ARRAY[
                               'code', 'message', 'phase',
                               'execution_started', 'retryable'
                           ])
                           OR diagnostic - 'code' - 'message' - 'phase'
                               - 'execution_started' - 'retryable' - 'detail' <> '{}'::jsonb
                           OR jsonb_typeof(diagnostic -> 'code') <> 'string'
                           OR diagnostic ->> 'code' !~ '^[A-Z][A-Z0-9_]{0,63}$'
                           OR jsonb_typeof(diagnostic -> 'message') <> 'string'
                           OR length(diagnostic ->> 'message') NOT BETWEEN 1 AND 2000
                           OR diagnostic ->> 'message' <> btrim(diagnostic ->> 'message')
                           OR diagnostic ->> 'phase' NOT IN (
                               'read', 'preflight', 'execution', 'output_validation'
                           )
                           OR jsonb_typeof(diagnostic -> 'execution_started') <> 'boolean'
                           OR jsonb_typeof(diagnostic -> 'retryable') <> 'boolean'
                           OR (
                               diagnostic ? 'detail'
                               AND jsonb_typeof(diagnostic -> 'detail')
                                   NOT IN ('object', 'null')
                           )
                           OR octet_length((diagnostic -> 'detail')::text) > 2000
                   ) THEN
                    RAISE EXCEPTION 'IFC query diagnostics exceed the safe bounded contract'
                        USING ERRCODE = '23514';
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
                       AND artifact.run_id = NEW.run_id
                       AND artifact.kind = 'ifc_query_source'
                       AND artifact.status IN ('ready', 'available')
                ) THEN
                    RAISE EXCEPTION 'IFC query exact source ArtifactVersion is invalid'
                        USING ERRCODE = '23503';
                END IF;

                IF NEW.result_artifact_version_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1
                      FROM artifact_versions AS version
                      JOIN artifacts AS artifact
                        ON artifact.owner_id = version.owner_id
                       AND artifact.task_id = version.task_id
                       AND artifact.artifact_id = version.artifact_id
                     WHERE version.owner_id = NEW.owner_id
                       AND version.tenant_id = NEW.tenant_id
                       AND version.task_id = NEW.task_id
                       AND version.artifact_id = NEW.result_artifact_id
                       AND version.version_id = NEW.result_artifact_version_id
                       AND artifact.run_id = NEW.run_id
                       AND artifact.kind = 'ifc_query_result'
                       AND artifact.status IN ('ready', 'available')
                ) THEN
                    RAISE EXCEPTION 'IFC query exact result ArtifactVersion is invalid'
                        USING ERRCODE = '23503';
                END IF;

                IF NEW.repair_parent_attempt_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM ifc_query_attempts AS parent
                     WHERE parent.owner_id = NEW.owner_id
                       AND parent.tenant_id = NEW.tenant_id
                       AND parent.task_id = NEW.task_id
                       AND parent.run_id = NEW.run_id
                       AND parent.attempt_id = NEW.repair_parent_attempt_id
                       AND parent.ifc_model_id = NEW.ifc_model_id
                       AND parent.ifc_revision_id = NEW.ifc_revision_id
                       AND parent.status IN ('SUCCEEDED', 'FAILED')
                ) THEN
                    RAISE EXCEPTION 'IFC query repair parent is not an exact terminal parent'
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
            "CREATE TRIGGER trg_ifc_query_attempt_guard "
            "BEFORE INSERT OR UPDATE ON ifc_query_attempts "
            "FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_ifc_query_attempt()"
        )
    )


def _create_input_guards() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_guard_ifc_query_input()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            DECLARE
                payload_row data_package_members%ROWTYPE;
                schema_row data_package_members%ROWTYPE;
            BEGIN
                IF TG_OP <> 'INSERT' THEN
                    RAISE EXCEPTION 'IFC query exact inputs are immutable'
                        USING ERRCODE = '23514';
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
                    RAISE EXCEPTION 'IFC query exact payload member is invalid'
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
                    RAISE EXCEPTION 'IFC query Package is not visible in this Run'
                        USING ERRCODE = '23503';
                END IF;

                IF payload_row.media_type = 'application/json' THEN
                    IF NEW.schema_member_id IS NULL THEN
                        RAISE EXCEPTION 'IFC query JSON payload requires an exact schema member'
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
                        RAISE EXCEPTION 'IFC query JSON schema member is invalid'
                            USING ERRCODE = '23503';
                    END IF;
                ELSIF payload_row.media_type = 'application/vnd.apache.parquet' THEN
                    IF NEW.schema_member_id IS NOT NULL THEN
                        RAISE EXCEPTION 'IFC query Parquet payload cannot bind a schema member'
                            USING ERRCODE = '23514';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'IFC query payload media type is unsupported'
                        USING ERRCODE = '23514';
                END IF;

                IF EXISTS (
                    SELECT 1 FROM ifc_query_attempts AS attempt
                     WHERE attempt.owner_id = NEW.owner_id
                       AND attempt.task_id = NEW.task_id
                       AND attempt.run_id = NEW.run_id
                       AND attempt.attempt_id = NEW.attempt_id
                       AND attempt.repair_parent_attempt_id IS NOT NULL
                ) AND NOT EXISTS (
                    SELECT 1
                      FROM ifc_query_attempts AS attempt
                      JOIN ifc_query_inputs AS parent_input
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
                       AND parent_input.schema_member_id IS NOT DISTINCT FROM NEW.schema_member_id
                ) THEN
                    RAISE EXCEPTION 'ifc query repair must preserve the exact input set'
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
            "CREATE TRIGGER trg_ifc_query_input_guard "
            "BEFORE INSERT OR UPDATE OR DELETE ON ifc_query_inputs "
            "FOR EACH ROW EXECUTE FUNCTION cad2bim_guard_ifc_query_input()"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_validate_ifc_query_input_set()
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
                  FROM ifc_query_inputs
                 WHERE owner_id = NEW.owner_id
                   AND task_id = NEW.task_id
                   AND run_id = NEW.run_id
                   AND attempt_id = NEW.attempt_id;
                IF bound_count > 16 OR (
                    bound_count > 0 AND (
                        minimum_ordinal <> 1 OR maximum_ordinal <> bound_count
                    )
                ) THEN
                    RAISE EXCEPTION 'IFC query exact input ordinals are not contiguous'
                        USING ERRCODE = '23514';
                END IF;
                IF bound_count <> NEW.input_count THEN
                    RAISE EXCEPTION 'IFC query exact input count does not match child bindings'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.repair_parent_attempt_id IS NOT NULL AND (
                    EXISTS (
                        SELECT ordinal, input_name, package_id, payload_member_id, schema_member_id
                          FROM ifc_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id AND attempt_id = NEW.attempt_id
                        EXCEPT
                        SELECT ordinal, input_name, package_id, payload_member_id, schema_member_id
                          FROM ifc_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id
                           AND attempt_id = NEW.repair_parent_attempt_id
                    ) OR EXISTS (
                        SELECT ordinal, input_name, package_id, payload_member_id, schema_member_id
                          FROM ifc_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id
                           AND attempt_id = NEW.repair_parent_attempt_id
                        EXCEPT
                        SELECT ordinal, input_name, package_id, payload_member_id, schema_member_id
                          FROM ifc_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id AND attempt_id = NEW.attempt_id
                    )
                ) THEN
                    RAISE EXCEPTION 'ifc query repair must preserve the exact input set'
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
            "CREATE CONSTRAINT TRIGGER trg_ifc_query_input_set "
            "AFTER INSERT OR UPDATE ON ifc_query_attempts "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION cad2bim_validate_ifc_query_input_set()"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_validate_ifc_query_input_parent()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            DECLARE
                attempt ifc_query_attempts%ROWTYPE;
                bound_count integer;
            BEGIN
                SELECT * INTO attempt
                  FROM ifc_query_attempts AS candidate
                 WHERE candidate.owner_id = NEW.owner_id
                   AND candidate.task_id = NEW.task_id
                   AND candidate.run_id = NEW.run_id
                   AND candidate.attempt_id = NEW.attempt_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'IFC query input lost its parent attempt'
                        USING ERRCODE = '23503';
                END IF;
                SELECT count(*) INTO bound_count
                  FROM ifc_query_inputs
                 WHERE owner_id = NEW.owner_id
                   AND task_id = NEW.task_id
                   AND run_id = NEW.run_id
                   AND attempt_id = NEW.attempt_id;
                IF bound_count <> attempt.input_count THEN
                    RAISE EXCEPTION 'IFC query exact input count does not match child bindings'
                        USING ERRCODE = '23514';
                END IF;
                IF attempt.repair_parent_attempt_id IS NOT NULL AND (
                    EXISTS (
                        SELECT ordinal, input_name, package_id, payload_member_id, schema_member_id
                          FROM ifc_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id AND attempt_id = NEW.attempt_id
                        EXCEPT
                        SELECT ordinal, input_name, package_id, payload_member_id, schema_member_id
                          FROM ifc_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id
                           AND attempt_id = attempt.repair_parent_attempt_id
                    ) OR EXISTS (
                        SELECT ordinal, input_name, package_id, payload_member_id, schema_member_id
                          FROM ifc_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id
                           AND attempt_id = attempt.repair_parent_attempt_id
                        EXCEPT
                        SELECT ordinal, input_name, package_id, payload_member_id, schema_member_id
                          FROM ifc_query_inputs
                         WHERE owner_id = NEW.owner_id AND task_id = NEW.task_id
                           AND run_id = NEW.run_id AND attempt_id = NEW.attempt_id
                    )
                ) THEN
                    RAISE EXCEPTION 'ifc query repair must preserve the exact input set'
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
            "CREATE CONSTRAINT TRIGGER trg_ifc_query_input_parent "
            "AFTER INSERT ON ifc_query_inputs "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION cad2bim_validate_ifc_query_input_parent()"
        )
    )


def _enable_rls_and_grants(schema: str) -> None:
    read_roles = ", ".join(_READ_ROLES)
    write_roles = ", ".join(_WRITE_ROLES)
    scope = (
        "tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '') AND "
        "task_id = NULLIF(current_setting('cad2bim.task_id', true), '') AND "
        "run_id = NULLIF(current_setting('cad2bim.run_id', true), '')"
    )
    for table in (_ATTEMPTS, _INPUTS):
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_runtime_select ON {table} "
                f"FOR SELECT TO {read_roles} USING ({scope})"
            )
        )
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_runtime_insert ON {table} "
                f"FOR INSERT TO {write_roles} WITH CHECK ({scope})"
            )
        )
        op.execute(sa.text(f"GRANT SELECT ON TABLE {table} TO {read_roles}"))
        op.execute(sa.text(f"GRANT INSERT ON TABLE {table} TO {write_roles}"))
    op.execute(
        sa.text(
            f"CREATE POLICY ifc_query_attempts_runtime_terminal ON {_ATTEMPTS} "
            f"FOR UPDATE TO {write_roles} USING ({scope}) "
            f"WITH CHECK ({scope} AND status IN ('SUCCEEDED', 'FAILED'))"
        )
    )
    for role in _WRITE_ROLES:
        op.execute(
            sa.text(
                "GRANT UPDATE (status, result_artifact_id, result_artifact_version_id, "
                f"diagnostics_json, finished_at) ON TABLE {_ATTEMPTS} TO {role}"
            )
        )

    op.execute(
        sa.text(
            "CREATE POLICY ifc_query_artifact_run_isolation ON artifacts "
            "AS RESTRICTIVE USING (kind NOT IN ('ifc_query_source', 'ifc_query_result') OR "
            "(task_id = NULLIF(current_setting('cad2bim.task_id', true), '') AND "
            "run_id = NULLIF(current_setting('cad2bim.run_id', true), ''))) "
            "WITH CHECK (kind NOT IN ('ifc_query_source', 'ifc_query_result') OR "
            "(task_id = NULLIF(current_setting('cad2bim.task_id', true), '') AND "
            "run_id = NULLIF(current_setting('cad2bim.run_id', true), '')))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY ifc_query_artifact_version_run_isolation ON artifact_versions "
            "AS RESTRICTIVE "
            f"USING ({schema}.cad2bim_ifc_query_artifact_visible(owner_id, task_id, artifact_id)) "
            f"WITH CHECK ({schema}.cad2bim_ifc_query_artifact_visible("
            "owner_id, task_id, artifact_id))"
        )
    )


def upgrade() -> None:
    schema = _quoted_schema()
    op.create_table(
        _ATTEMPTS,
        sa.Column("attempt_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("ifc_model_id", sa.String(length=80), nullable=False),
        sa.Column("ifc_revision_id", sa.String(length=80), nullable=False),
        sa.Column("source_artifact_id", sa.String(length=80), nullable=False),
        sa.Column("source_artifact_version_id", sa.String(length=80), nullable=False),
        sa.Column("result_artifact_id", sa.String(length=80)),
        sa.Column("result_artifact_version_id", sa.String(length=80)),
        sa.Column("repair_parent_attempt_id", sa.String(length=80)),
        sa.Column("request_identity", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("input_count", sa.Integer(), nullable=False),
        sa.Column("budget_json", postgresql.JSONB(none_as_null=True), nullable=False),
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
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_ifc_query_attempts_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_ifc_query_attempts_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_ifc_query_attempts_owner_task_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "ifc_model_id", "ifc_revision_id"],
            [
                "ifc_model_revisions.owner_id",
                "ifc_model_revisions.task_id",
                "ifc_model_revisions.run_id",
                "ifc_model_revisions.ifc_model_id",
                "ifc_model_revisions.revision_id",
            ],
            name="fk_ifc_query_attempts_exact_ifc_revision",
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
            name="fk_ifc_query_attempts_run_source_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "result_artifact_id"],
            [
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ],
            name="fk_ifc_query_attempts_run_result_artifact",
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
            name="fk_ifc_query_attempts_exact_source_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "result_artifact_id", "result_artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_ifc_query_attempts_exact_result_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "owner_id",
                "task_id",
                "run_id",
                "repair_parent_attempt_id",
                "ifc_model_id",
                "ifc_revision_id",
            ],
            [
                "ifc_query_attempts.owner_id",
                "ifc_query_attempts.task_id",
                "ifc_query_attempts.run_id",
                "ifc_query_attempts.attempt_id",
                "ifc_query_attempts.ifc_model_id",
                "ifc_query_attempts.ifc_revision_id",
            ],
            name="fk_ifc_query_attempts_exact_repair_parent",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "status IN ('PREPARED', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_ifc_query_attempts_status_valid"),
        ),
        sa.CheckConstraint(
            "request_identity <> '' AND request_identity = trim(request_identity)",
            name=op.f("ck_ifc_query_attempts_request_identity_valid"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("request_digest"),
            name=op.f("ck_ifc_query_attempts_request_digest_shape"),
        ),
        sa.CheckConstraint(
            "repair_parent_attempt_id IS NULL OR repair_parent_attempt_id <> attempt_id",
            name=op.f("ck_ifc_query_attempts_repair_parent_not_self"),
        ),
        sa.CheckConstraint(
            "input_count BETWEEN 0 AND 16",
            name=op.f("ck_ifc_query_attempts_input_count_bounded"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(budget_json) = 'object'",
            name=op.f("ck_ifc_query_attempts_budget_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(diagnostics_json) = 'array'",
            name=op.f("ck_ifc_query_attempts_diagnostics_array"),
        ),
        sa.CheckConstraint(
            "jsonb_array_length(diagnostics_json) <= 32",
            name=op.f("ck_ifc_query_attempts_diagnostics_count_bounded"),
        ),
        sa.CheckConstraint(
            "(result_artifact_id IS NULL) = (result_artifact_version_id IS NULL)",
            name=op.f("ck_ifc_query_attempts_result_artifact_pair_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'PREPARED' AND result_artifact_version_id IS NULL "
            "AND diagnostics_json = '[]'::jsonb AND finished_at IS NULL) OR "
            "(status = 'SUCCEEDED' AND result_artifact_version_id IS NOT NULL "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'FAILED' AND result_artifact_version_id IS NULL "
            "AND jsonb_array_length(diagnostics_json) > 0 AND finished_at IS NOT NULL)",
            name=op.f("ck_ifc_query_attempts_terminal_shape_valid"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= created_at",
            name=op.f("ck_ifc_query_attempts_time_order_valid"),
        ),
        sa.PrimaryKeyConstraint("attempt_id", name=op.f("pk_ifc_query_attempts")),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "attempt_id",
            name="uq_ifc_query_attempts_scope_attempt",
        ),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "attempt_id",
            "ifc_model_id",
            "ifc_revision_id",
            name="uq_ifc_query_attempts_exact_revision_attempt",
        ),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "request_identity",
            name="uq_ifc_query_attempts_run_request_identity",
        ),
    )
    op.create_index("ix_ifc_query_attempts_tenant_id", _ATTEMPTS, ("tenant_id",))
    op.create_index(
        "ix_ifc_query_attempts_scope_created",
        _ATTEMPTS,
        ("owner_id", "task_id", "run_id", "created_at"),
    )

    op.create_table(
        _INPUTS,
        sa.Column("attempt_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("input_name", sa.String(length=63), nullable=False),
        sa.Column("package_id", sa.String(length=80), nullable=False),
        sa.Column("payload_member_id", sa.String(length=80), nullable=False),
        sa.Column("schema_member_id", sa.String(length=80)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_ifc_query_inputs_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_ifc_query_inputs_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "attempt_id"],
            [
                "ifc_query_attempts.owner_id",
                "ifc_query_attempts.task_id",
                "ifc_query_attempts.run_id",
                "ifc_query_attempts.attempt_id",
            ],
            name="fk_ifc_query_inputs_scope_attempt",
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
            name="fk_ifc_query_inputs_exact_payload_member",
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
            name="fk_ifc_query_inputs_exact_schema_member",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "ordinal BETWEEN 1 AND 16", name=op.f("ck_ifc_query_inputs_ordinal_bounded")
        ),
        sa.CheckConstraint(
            "input_name ~ '^[a-z][a-z0-9_]{0,62}$'",
            name=op.f("ck_ifc_query_inputs_input_name_valid"),
        ),
        sa.PrimaryKeyConstraint("attempt_id", "ordinal", name=op.f("pk_ifc_query_inputs")),
        sa.UniqueConstraint("attempt_id", "ordinal", name="uq_ifc_query_inputs_attempt_ordinal"),
        sa.UniqueConstraint("attempt_id", "input_name", name="uq_ifc_query_inputs_attempt_name"),
        sa.UniqueConstraint(
            "attempt_id",
            "package_id",
            "payload_member_id",
            name="uq_ifc_query_inputs_attempt_payload",
        ),
    )
    op.create_index("ix_ifc_query_inputs_tenant_id", _INPUTS, ("tenant_id",))
    op.create_index(
        "ix_ifc_query_inputs_scope_attempt",
        _INPUTS,
        ("owner_id", "task_id", "attempt_id"),
    )

    _create_artifact_visibility(schema)
    _create_query_artifact_guards()
    _create_attempt_guard()
    _create_input_guards()
    _enable_rls_and_grants(schema)


def downgrade() -> None:
    schema = _quoted_schema()
    op.execute(
        sa.text(
            "LOCK TABLE ifc_query_inputs, ifc_query_attempts, artifacts, "
            "artifact_versions IN ACCESS EXCLUSIVE MODE"
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                attempt_count bigint;
                artifact_count bigint;
            BEGIN
                SELECT count(*) INTO attempt_count FROM ifc_query_attempts;
                SELECT count(*) INTO artifact_count FROM artifacts
                 WHERE kind IN ('ifc_query_source', 'ifc_query_result');
                IF attempt_count <> 0 OR artifact_count <> 0 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = format(
                            'IFC query history is not empty: attempts=%s, artifacts=%s',
                            attempt_count, artifact_count
                        );
                END IF;
            END
            $$
            """
        )
    )
    op.execute(sa.text("DROP POLICY ifc_query_artifact_version_run_isolation ON artifact_versions"))
    op.execute(sa.text("DROP POLICY ifc_query_artifact_run_isolation ON artifacts"))
    op.execute(sa.text("DROP TRIGGER trg_ifc_query_artifact_version_guard ON artifact_versions"))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_ifc_query_artifact_version()"))
    op.execute(sa.text("DROP TRIGGER trg_ifc_query_artifact_guard ON artifacts"))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_ifc_query_artifact()"))
    for table in (_INPUTS, _ATTEMPTS):
        for role in _WRITE_ROLES:
            op.execute(sa.text(f"REVOKE INSERT ON TABLE {table} FROM {role}"))
        for role in _READ_ROLES:
            op.execute(sa.text(f"REVOKE SELECT ON TABLE {table} FROM {role}"))
    for role in _WRITE_ROLES:
        op.execute(
            sa.text(
                "REVOKE UPDATE (status, result_artifact_id, result_artifact_version_id, "
                f"diagnostics_json, finished_at) ON TABLE {_ATTEMPTS} FROM {role}"
            )
        )
    op.execute(sa.text("DROP TRIGGER trg_ifc_query_input_parent ON ifc_query_inputs"))
    op.execute(sa.text("DROP FUNCTION cad2bim_validate_ifc_query_input_parent()"))
    op.execute(sa.text("DROP TRIGGER trg_ifc_query_input_set ON ifc_query_attempts"))
    op.execute(sa.text("DROP FUNCTION cad2bim_validate_ifc_query_input_set()"))
    op.execute(sa.text("DROP TRIGGER trg_ifc_query_input_guard ON ifc_query_inputs"))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_ifc_query_input()"))
    op.execute(sa.text("DROP TRIGGER trg_ifc_query_attempt_guard ON ifc_query_attempts"))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_ifc_query_attempt()"))
    op.drop_table(_INPUTS)
    op.drop_table(_ATTEMPTS)
    signature = "cad2bim_ifc_query_artifact_visible(text,text,text)"
    for role in _READ_ROLES:
        op.execute(sa.text(f"REVOKE EXECUTE ON FUNCTION {schema}.{signature} FROM {role}"))
    op.execute(sa.text(f"DROP FUNCTION {schema}.{signature}"))
