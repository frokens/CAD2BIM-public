"""Add lease-fenced durable defer for exact MCP restore recovery.

Revision ID: 20260727_0026
Revises: 20260719_0025
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op


revision: str = "20260727_0026"
down_revision: str | None = "20260719_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _quoted_schema() -> str:
    if context.is_offline_mode():
        return "public"
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    return bind.dialect.identifier_preparer.quote(schema)


def _create_claim_function(schema: str, *, durable_restore_defer: bool) -> None:
    queued_filter = (
        """
                       (
                           candidate.status = 'queued'
                           AND (
                               candidate.claim_not_before IS NULL
                               OR candidate.claim_not_before <= v_now
                           )
                       )"""
        if durable_restore_defer
        else "candidate.status = 'queued'"
    )
    recovery_select = (
        """,
                       COALESCE(
                           candidate.restore_defer_reason_code = 'mcp_restore_transient',
                           false
                       )"""
        if durable_restore_defer
        else ""
    )
    recovery_into = (
        """,
                       v_deferred_restore"""
        if durable_restore_defer
        else ""
    )
    recovery_update = (
        """,
                       recovery_generation = CASE
                           WHEN v_was_running
                           THEN claimed.recovery_generation + 1
                           ELSE claimed.recovery_generation
                       END,
                       restore_defer_count = CASE
                           WHEN v_was_running
                           THEN 0
                           ELSE claimed.restore_defer_count
                       END,
                       claim_not_before = NULL,
                       restore_defer_reason_code = NULL"""
        if durable_restore_defer
        else ""
    )
    recovery_declare = (
        """
                v_deferred_restore boolean;"""
        if durable_restore_defer
        else ""
    )
    reclaimed_assignment = (
        "v_was_running OR v_deferred_restore"
        if durable_restore_defer
        else "v_was_running"
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
                reclaimed_lease boolean,
                writer_generation bigint
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
                v_was_running boolean;
                v_reclaimed_lease boolean;
                v_writer_generation bigint;
                v_now timestamptz := clock_timestamp();{recovery_declare}
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
                       candidate.status = 'running'{recovery_select}
                  INTO v_run_id,
                       v_task_id,
                       v_owner_id,
                       v_tenant_id,
                       v_principal_id,
                       v_was_running{recovery_into}
                  FROM runs AS candidate
                  JOIN task_tenant_bindings AS binding
                    ON binding.tenant_id = candidate.tenant_id
                   AND binding.task_id = candidate.task_id
                 WHERE candidate.execution_protocol_status = 'pinned'
                   AND candidate.tenant_id IS NOT NULL
                   AND candidate.owner_id = candidate.tenant_id
                   AND (
                       {queued_filter}
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

                v_reclaimed_lease := {reclaimed_assignment};
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
                       writer_generation = claimed.writer_generation + 1,
                       started_at = COALESCE(claimed.started_at, v_now),
                       updated_at = v_now{recovery_update}
                 WHERE claimed.run_id = v_run_id
                 RETURNING claimed.writer_generation INTO v_writer_generation;

                run_id := v_run_id;
                task_id := v_task_id;
                owner_id := v_owner_id;
                tenant_id := v_tenant_id;
                principal_id := v_principal_id;
                lease_token := v_lease_token;
                reclaimed_lease := v_reclaimed_lease;
                writer_generation := v_writer_generation;
                RETURN NEXT;
            END
            $function$
            """
        )
    )


def _replace_claim_function(schema: str, *, durable_restore_defer: bool) -> None:
    op.execute(sa.text(f"DROP FUNCTION {schema}.cad2bim_claim_next_run(text,integer)"))
    _create_claim_function(schema, durable_restore_defer=durable_restore_defer)
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


def upgrade() -> None:
    schema = _quoted_schema()
    op.add_column(
        "runs",
        sa.Column("claim_not_before", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column(
            "recovery_generation",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "restore_defer_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "runs",
        sa.Column("restore_defer_reason_code", sa.String(100), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_runs_recovery_generation_nonnegative"),
        "runs",
        "recovery_generation >= 0",
    )
    op.create_check_constraint(
        op.f("ck_runs_restore_defer_count_nonnegative"),
        "runs",
        "restore_defer_count >= 0",
    )
    op.drop_index("ix_runs_claim_queue", table_name="runs")
    op.create_index(
        "ix_runs_claim_queue",
        "runs",
        ("status", "claim_not_before", "lease_expires_at", "created_at"),
    )
    _replace_claim_function(schema, durable_restore_defer=True)


def downgrade() -> None:
    schema = _quoted_schema()
    _replace_claim_function(schema, durable_restore_defer=False)
    op.drop_index("ix_runs_claim_queue", table_name="runs")
    op.create_index(
        "ix_runs_claim_queue",
        "runs",
        ("status", "lease_expires_at", "created_at"),
    )
    op.drop_constraint(
        op.f("ck_runs_restore_defer_count_nonnegative"),
        "runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_runs_recovery_generation_nonnegative"),
        "runs",
        type_="check",
    )
    op.drop_column("runs", "restore_defer_reason_code")
    op.drop_column("runs", "restore_defer_count")
    op.drop_column("runs", "recovery_generation")
    op.drop_column("runs", "claim_not_before")
