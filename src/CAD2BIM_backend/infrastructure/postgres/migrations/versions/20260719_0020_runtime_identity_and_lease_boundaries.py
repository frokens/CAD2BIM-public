"""Add controlled identity resolution and global Runner lease claim commands.

Revision ID: 20260719_0020
Revises: 20260719_0019
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op


revision: str = "20260719_0020"
down_revision: str | None = "20260719_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _quoted_schema() -> str:
    if context.is_offline_mode():
        return "public"
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    return bind.dialect.identifier_preparer.quote(schema)


def upgrade() -> None:
    schema = _quoted_schema()
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION {schema}.cad2bim_resolve_identity(
                p_issuer text,
                p_subject text,
                p_expected_principal_id text,
                p_expected_tenant_id text
            )
            RETURNS TABLE(principal_id text, tenant_id text)
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = {schema}, pg_temp
            AS $function$
            DECLARE
                v_principal_id text;
                v_principal_status text;
                v_tenant_id text;
                v_tenant_status text;
                v_membership_tenant_id text;
            BEGIN
                IF p_issuer IS NULL OR btrim(p_issuer) = '' OR length(p_issuer) > 512
                   OR p_subject IS NULL OR btrim(p_subject) = '' OR length(p_subject) > 255
                   OR p_expected_principal_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$'
                   OR p_expected_tenant_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$'
                   OR p_expected_principal_id = p_expected_tenant_id THEN
                    RAISE EXCEPTION 'identity_access_denied' USING ERRCODE = 'P0001';
                END IF;

                INSERT INTO principals (
                    principal_id, issuer, subject, status, legacy_owner_id,
                    created_at, updated_at
                ) VALUES (
                    p_expected_principal_id, p_issuer, p_subject, 'active', NULL,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (issuer, subject) DO NOTHING;

                SELECT candidate.principal_id, candidate.status
                  INTO v_principal_id, v_principal_status
                  FROM principals AS candidate
                 WHERE candidate.issuer = p_issuer
                   AND candidate.subject = p_subject;
                IF v_principal_id IS NULL
                   OR v_principal_id <> p_expected_principal_id
                   OR v_principal_status <> 'active' THEN
                    RAISE EXCEPTION 'identity_access_denied' USING ERRCODE = 'P0001';
                END IF;

                INSERT INTO tenants (
                    tenant_id, kind, status, legacy_owner_id, created_at, updated_at
                ) VALUES (
                    p_expected_tenant_id, 'personal', 'active', NULL,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT ON CONSTRAINT pk_tenants DO NOTHING;

                INSERT INTO tenant_principals (
                    tenant_id, principal_id, access, created_at
                ) VALUES (
                    p_expected_tenant_id, v_principal_id, 'owner', CURRENT_TIMESTAMP
                )
                ON CONFLICT DO NOTHING;

                SELECT membership.tenant_id
                  INTO v_membership_tenant_id
                  FROM tenant_principals AS membership
                 WHERE membership.principal_id = v_principal_id;
                SELECT tenant.tenant_id, tenant.status
                  INTO v_tenant_id, v_tenant_status
                  FROM tenants AS tenant
                 WHERE tenant.tenant_id = p_expected_tenant_id;
                IF v_membership_tenant_id IS DISTINCT FROM p_expected_tenant_id
                   OR v_tenant_id IS NULL
                   OR v_tenant_status <> 'active' THEN
                    RAISE EXCEPTION 'identity_access_denied' USING ERRCODE = 'P0001';
                END IF;

                INSERT INTO users (
                    owner_id, tenant_id, created_at, updated_at
                ) VALUES (
                    p_expected_tenant_id, p_expected_tenant_id,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT ON CONSTRAINT pk_users DO NOTHING;
                IF NOT EXISTS (
                    SELECT 1 FROM users AS owner_alias
                     WHERE owner_alias.owner_id = p_expected_tenant_id
                       AND owner_alias.tenant_id = p_expected_tenant_id
                ) THEN
                    RAISE EXCEPTION 'identity_access_denied' USING ERRCODE = 'P0001';
                END IF;

                RETURN QUERY SELECT v_principal_id, v_tenant_id;
            END
            $function$
            """
        )
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
                reclaimed_lease boolean
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
                v_now timestamptz := clock_timestamp();
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
                       updated_at = v_now
                 WHERE claimed.run_id = v_run_id;

                run_id := v_run_id;
                task_id := v_task_id;
                owner_id := v_owner_id;
                tenant_id := v_tenant_id;
                principal_id := v_principal_id;
                lease_token := v_lease_token;
                reclaimed_lease := v_reclaimed_lease;
                RETURN NEXT;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION "
            f"{schema}.cad2bim_resolve_identity(text,text,text,text) FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            f"GRANT EXECUTE ON FUNCTION "
            f"{schema}.cad2bim_resolve_identity(text,text,text,text) "
            "TO cad2bim_api_runtime"
        )
    )
    op.execute(
        sa.text(
            "GRANT SELECT ON TABLE alembic_version "
            "TO cad2bim_api_runtime, cad2bim_runner_runtime"
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION "
            f"{schema}.cad2bim_claim_next_run(text,integer) FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            f"GRANT EXECUTE ON FUNCTION "
            f"{schema}.cad2bim_claim_next_run(text,integer) "
            "TO cad2bim_runner_runtime"
        )
    )


def downgrade() -> None:
    schema = _quoted_schema()
    op.execute(
        sa.text(
            "REVOKE SELECT ON TABLE alembic_version "
            "FROM cad2bim_api_runtime, cad2bim_runner_runtime"
        )
    )
    op.execute(
        sa.text(
            f"DROP FUNCTION {schema}.cad2bim_claim_next_run(text,integer)"
        )
    )
    op.execute(
        sa.text(
            f"DROP FUNCTION {schema}.cad2bim_resolve_identity(text,text,text,text)"
        )
    )
