"""Add one authoritative writer generation across Run and MCP session leases.

Revision ID: 20260719_0022
Revises: 20260719_0021
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op


revision: str = "20260719_0022"
down_revision: str | None = "20260719_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _quoted_schema() -> str:
    if context.is_offline_mode():
        return "public"
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    return bind.dialect.identifier_preparer.quote(schema)


def _create_claim_function(schema: str, *, with_generation: bool) -> None:
    generation_return = ",\n                writer_generation bigint" if with_generation else ""
    generation_declare = "\n                v_writer_generation bigint;" if with_generation else ""
    generation_update = (
        ",\n                       writer_generation = claimed.writer_generation + 1"
        if with_generation
        else ""
    )
    generation_returning = (
        "\n                 RETURNING claimed.writer_generation INTO v_writer_generation;"
        if with_generation
        else ";"
    )
    generation_assign = (
        "\n                writer_generation := v_writer_generation;" if with_generation else ""
    )
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {schema}.cad2bim_claim_next_run(
                p_runner_id text,
                p_lease_seconds integer
            )
            RETURNS TABLE(
                run_id text,
                task_id text,
                owner_id text,
                tenant_id text,
                principal_id text,
                lease_token text,
                reclaimed_lease boolean{generation_return}
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = {schema}, pg_temp
            AS $function$
            DECLARE
                v_run_id text;
                v_task_id text;
                v_owner_id text;
                v_tenant_id text;
                v_principal_id text;
                v_lease_token text;
                v_reclaimed_lease boolean;
                v_now timestamptz := clock_timestamp();{generation_declare}
            BEGIN
                IF p_runner_id IS NULL OR btrim(p_runner_id) = ''
                   OR length(p_runner_id) > 255
                   OR p_lease_seconds IS NULL OR p_lease_seconds <= 0
                   OR p_lease_seconds > 3600 THEN
                    RAISE EXCEPTION 'runner_claim_rejected' USING ERRCODE = 'P0001';
                END IF;

                SELECT candidate.run_id,
                       candidate.task_id,
                       candidate.owner_id,
                       candidate.tenant_id,
                       binding.principal_id,
                       candidate.status = 'running'
                  INTO v_run_id,
                       v_task_id,
                       v_owner_id,
                       v_tenant_id,
                       v_principal_id,
                       v_reclaimed_lease
                  FROM runs AS candidate
                  JOIN task_tenant_bindings AS binding
                    ON binding.tenant_id = candidate.tenant_id
                   AND binding.task_id = candidate.task_id
                 WHERE candidate.execution_protocol_status = 'pinned'
                   AND candidate.tenant_id IS NOT NULL
                   AND candidate.owner_id = candidate.tenant_id
                   AND (
                       candidate.status = 'queued'
                       OR (
                           candidate.status = 'running'
                           AND candidate.lease_expires_at IS NOT NULL
                           AND candidate.lease_expires_at <= v_now
                       )
                   )
                 ORDER BY candidate.created_at, candidate.run_id
                 FOR UPDATE OF candidate SKIP LOCKED
                 LIMIT 1;

                IF v_run_id IS NULL THEN
                    RETURN;
                END IF;

                v_lease_token := 'lease_' || encode(
                    sha256(convert_to(
                        p_runner_id || ':' || v_run_id || ':' ||
                        v_now::text || ':' || random()::text,
                        'UTF8'
                    )),
                    'hex'
                );
                UPDATE runs AS claimed
                   SET status = 'running',
                       claimed_by = p_runner_id,
                       lease_token = v_lease_token,
                       lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
                       heartbeat_at = v_now,
                       claim_count = claimed.claim_count + 1,
                       reclaimed_lease = v_reclaimed_lease,
                       started_at = COALESCE(claimed.started_at, v_now),
                       updated_at = v_now{generation_update}
                 WHERE claimed.run_id = v_run_id{generation_returning}

                run_id := v_run_id;
                task_id := v_task_id;
                owner_id := v_owner_id;
                tenant_id := v_tenant_id;
                principal_id := v_principal_id;
                lease_token := v_lease_token;
                reclaimed_lease := v_reclaimed_lease;{generation_assign}
                RETURN NEXT;
            END
            $function$
            """
        )
    )


def upgrade() -> None:
    schema = _quoted_schema()
    op.add_column(
        "runs",
        sa.Column("writer_generation", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        op.f("ck_runs_writer_generation_nonnegative"),
        "runs",
        "writer_generation >= 0",
    )
    op.add_column(
        "mcp_sessions",
        sa.Column("writer_generation", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        op.f("ck_mcp_sessions_writer_generation_nonnegative"),
        "mcp_sessions",
        "writer_generation >= 0",
    )
    for table_name in ("mcp_snapshots", "checkpoint_links", "safe_point_publications"):
        op.add_column(
            table_name,
            sa.Column(
                "writer_generation",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            ),
        )
        op.create_check_constraint(
            op.f(f"ck_{table_name}_writer_generation_nonnegative"),
            table_name,
            "writer_generation >= 0",
        )

    op.execute(
        sa.text(f"DROP FUNCTION {schema}.cad2bim_claim_next_run(text,integer)")
    )
    _create_claim_function(schema, with_generation=True)
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {schema}.cad2bim_claim_next_run(text,integer) "
            "FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            f"GRANT EXECUTE ON FUNCTION {schema}.cad2bim_claim_next_run(text,integer) "
            "TO cad2bim_runner_runtime"
        )
    )

    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {schema}.cad2bim_validate_writer_fence(
                p_owner_id text,
                p_task_id text,
                p_run_id text,
                p_session_id text,
                p_writer_id text,
                p_writer_generation bigint
            )
            RETURNS boolean
            LANGUAGE sql
            VOLATILE
            SECURITY DEFINER
            SET search_path = {schema}, pg_temp
            AS $function$
                SELECT CASE
                    WHEN p_owner_id IS NULL OR btrim(p_owner_id) = ''
                      OR p_task_id IS NULL OR btrim(p_task_id) = ''
                      OR p_run_id IS NULL OR btrim(p_run_id) = ''
                      OR p_session_id IS NULL OR btrim(p_session_id) = ''
                      OR p_writer_id IS NULL OR btrim(p_writer_id) = ''
                      OR p_writer_generation IS NULL OR p_writer_generation < 1
                    THEN false
                    ELSE EXISTS (
                        SELECT 1
                          FROM runs AS run
                          JOIN mcp_sessions AS mcp
                            ON mcp.owner_id = run.owner_id
                           AND mcp.task_id = run.task_id
                           AND mcp.run_id = run.run_id
                         WHERE run.owner_id = p_owner_id
                           AND run.task_id = p_task_id
                           AND run.run_id = p_run_id
                           AND run.status = 'running'
                           AND run.claimed_by = p_writer_id
                           AND run.writer_generation = p_writer_generation
                           AND run.lease_expires_at > clock_timestamp()
                           AND mcp.session_id = p_session_id
                           AND mcp.status = 'active'
                           AND mcp.writer_id = p_writer_id
                           AND mcp.writer_generation = p_writer_generation
                           AND mcp.writer_lease_expires_at > clock_timestamp()
                    )
                END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {schema}.cad2bim_validate_writer_fence"
            "(text,text,text,text,text,bigint) FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            f"GRANT EXECUTE ON FUNCTION {schema}.cad2bim_validate_writer_fence"
            "(text,text,text,text,text,bigint) TO cad2bim_api_runtime, cad2bim_runner_runtime"
        )
    )


def downgrade() -> None:
    schema = _quoted_schema()
    op.execute(
        sa.text(
            f"DROP FUNCTION {schema}.cad2bim_validate_writer_fence"
            "(text,text,text,text,text,bigint)"
        )
    )
    op.execute(
        sa.text(f"DROP FUNCTION {schema}.cad2bim_claim_next_run(text,integer)")
    )
    _create_claim_function(schema, with_generation=False)
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {schema}.cad2bim_claim_next_run(text,integer) "
            "FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            f"GRANT EXECUTE ON FUNCTION {schema}.cad2bim_claim_next_run(text,integer) "
            "TO cad2bim_runner_runtime"
        )
    )
    for table_name in reversed(
        ("mcp_snapshots", "checkpoint_links", "safe_point_publications")
    ):
        op.drop_constraint(
            op.f(f"ck_{table_name}_writer_generation_nonnegative"),
            table_name,
            type_="check",
        )
        op.drop_column(table_name, "writer_generation")
    op.drop_constraint(
        op.f("ck_mcp_sessions_writer_generation_nonnegative"),
        "mcp_sessions",
        type_="check",
    )
    op.drop_column("mcp_sessions", "writer_generation")
    op.drop_constraint(
        op.f("ck_runs_writer_generation_nonnegative"),
        "runs",
        type_="check",
    )
    op.drop_column("runs", "writer_generation")
