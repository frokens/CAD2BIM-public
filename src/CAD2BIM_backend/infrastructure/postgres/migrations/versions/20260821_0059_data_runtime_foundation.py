"""Add Direct Run mode and the immutable Data Runtime catalog.

Revision ID: 20260821_0059
Revises: 20260815_0058
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op


revision: str = "20260821_0059"
down_revision: str | None = "20260815_0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


DATA_TABLES = (
    "direct_session_bindings",
    "data_packages",
    "data_package_members",
    "critical_operations",
    "data_catalog_entries",
)


def _quoted_schema() -> str:
    if context.is_offline_mode():
        return "public"
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema() ")).scalar_one()
    return bind.dialect.identifier_preparer.quote(schema)


def _create_mcp_runtime_role() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_roles WHERE rolname = 'cad2bim_mcp_runtime'
                ) THEN
                    CREATE ROLE cad2bim_mcp_runtime
                        NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                        NOINHERIT NOREPLICATION NOBYPASSRLS;
                ELSE
                    ALTER ROLE cad2bim_mcp_runtime
                        NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                        NOINHERIT NOREPLICATION NOBYPASSRLS;
                END IF;
                EXECUTE format(
                    'GRANT USAGE ON SCHEMA %I TO cad2bim_mcp_runtime',
                    current_schema()
                );
            END
            $$
            """
        )
    )


def _create_claim_function(schema: str, *, filter_graph_mode: bool) -> None:
    graph_filter = "AND candidate.execution_mode = 'graph'" if filter_graph_mode else ""
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
                v_deferred_restore boolean;
                v_reclaimed_lease boolean;
                v_writer_generation bigint;
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
                       candidate.status = 'running',
                       COALESCE(
                           candidate.restore_defer_reason_code = 'mcp_restore_transient',
                           false
                       )
                  INTO v_run_id,
                       v_task_id,
                       v_owner_id,
                       v_tenant_id,
                       v_principal_id,
                       v_was_running,
                       v_deferred_restore
                  FROM runs AS candidate
                  JOIN task_tenant_bindings AS binding
                    ON binding.tenant_id = candidate.tenant_id
                   AND binding.task_id = candidate.task_id
                 WHERE candidate.execution_protocol_status = 'pinned'
                   {graph_filter}
                   AND candidate.tenant_id IS NOT NULL
                   AND candidate.owner_id = candidate.tenant_id
                   AND (
                       (
                           candidate.status = 'queued'
                           AND (
                               candidate.claim_not_before IS NULL
                               OR candidate.claim_not_before <= v_now
                           )
                       )
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

                v_reclaimed_lease := v_was_running OR v_deferred_restore;
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
                       updated_at = v_now,
                       recovery_generation = CASE
                           WHEN v_was_running
                           THEN claimed.recovery_generation + 1
                           ELSE claimed.recovery_generation
                       END,
                       restore_defer_count = CASE
                           WHEN v_was_running THEN 0 ELSE claimed.restore_defer_count
                       END,
                       claim_not_before = NULL,
                       restore_defer_reason_code = NULL
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


def _replace_claim_function(schema: str, *, filter_graph_mode: bool) -> None:
    op.execute(sa.text(f"DROP FUNCTION {schema}.cad2bim_claim_next_run(text,integer)"))
    _create_claim_function(schema, filter_graph_mode=filter_graph_mode)
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {schema}.cad2bim_claim_next_run(text,integer) FROM PUBLIC")
    )
    op.execute(
        sa.text(
            f"GRANT EXECUTE ON FUNCTION {schema}.cad2bim_claim_next_run(text,integer) "
            "TO cad2bim_runner_runtime"
        )
    )


def _tenant_table(table: str) -> None:
    expression = "tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '')"
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({expression}) WITH CHECK ({expression})"
        )
    )


def upgrade() -> None:
    schema = _quoted_schema()
    _create_mcp_runtime_role()

    op.add_column(
        "runs",
        sa.Column(
            "execution_mode",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'graph'"),
        ),
    )
    op.drop_constraint(
        # Migration 0016 supplied an already-prefixed name without ``op.f``;
        # the active naming convention therefore persisted this exact double prefix.
        op.f("ck_runs_ck_runs_execution_protocol_mapping_valid"),
        "runs",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_runs_execution_protocol_mapping_valid"),
        "runs",
        "(execution_mode = 'graph' AND ((execution_protocol_status = 'pinned' "
        "AND execution_protocol IS NOT NULL AND execution_protocol_id IS NOT NULL "
        "AND execution_protocol_digest IS NOT NULL "
        "AND execution_protocol_quarantine_reason IS NULL) OR "
        "(execution_protocol_status = 'quarantined' AND execution_protocol IS NULL "
        "AND execution_protocol_id IS NULL AND execution_protocol_digest IS NULL "
        "AND execution_protocol_quarantine_reason IS NOT NULL))) OR "
        "(execution_mode = 'direct_mcp' "
        "AND execution_protocol_status = 'not_applicable' "
        "AND execution_protocol IS NULL AND execution_protocol_id IS NULL "
        "AND execution_protocol_digest IS NULL "
        "AND execution_protocol_quarantine_reason IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_runs_direct_run_not_runner_claimable"),
        "runs",
        "execution_mode = 'graph' OR (status <> 'queued' AND claimed_by IS NULL "
        "AND lease_token IS NULL AND lease_expires_at IS NULL AND heartbeat_at IS NULL "
        "AND claim_not_before IS NULL AND claim_count = 0)",
    )
    op.drop_index("ix_runs_claim_queue", table_name="runs")
    op.create_index(
        "ix_runs_claim_queue",
        "runs",
        ("execution_mode", "status", "claim_not_before", "lease_expires_at", "created_at"),
    )

    op.create_table(
        "direct_session_bindings",
        sa.Column("session_id", sa.String(length=80), nullable=False),
        sa.Column("direct_session_alias", sa.String(length=32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("configured_subject", sa.String(length=255), nullable=False),
        sa.Column("session_name", sa.String(length=100), nullable=False),
        sa.Column("normalized_session_name", sa.String(length=100), nullable=False),
        sa.Column("principal_id", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_direct_session_bindings_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_direct_session_bindings_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_direct_session_bindings_owner_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_direct_session_bindings_owner_task_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["tenant_principals.tenant_id", "tenant_principals.principal_id"],
            name="fk_direct_session_bindings_tenant_principal",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_direct_session_bindings")),
        sa.UniqueConstraint(
            "tenant_id",
            "configured_subject",
            "normalized_session_name",
            name="uq_direct_session_bindings_subject_name",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "configured_subject",
            "direct_session_alias",
            name="uq_direct_session_bindings_subject_alias",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "configured_subject",
            "ordinal",
            name="uq_direct_session_bindings_subject_ordinal",
        ),
    )
    op.create_index(
        op.f("ix_direct_session_bindings_tenant_id"),
        "direct_session_bindings",
        ["tenant_id"],
    )

    op.create_table(
        "data_packages",
        sa.Column("package_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("created_by_run_id", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("package_kind", sa.String(length=64), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.String(length=1000), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_data_packages_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_data_packages_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_data_packages_owner_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "created_by_run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_data_packages_owner_task_run",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "package_kind = 'source_l0'", name=op.f("ck_data_packages_package_kind_valid")
        ),
        sa.PrimaryKeyConstraint("package_id", name=op.f("pk_data_packages")),
        sa.UniqueConstraint(
            "owner_id", "task_id", "package_id", name="uq_data_packages_owner_task_package"
        ),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "package_id",
            "request_fingerprint",
            name="uq_data_packages_scope_fingerprint",
        ),
        sa.UniqueConstraint("task_id", "idempotency_key", name="uq_data_packages_task_idempotency"),
    )
    op.create_index(op.f("ix_data_packages_tenant_id"), "data_packages", ["tenant_id"])
    op.create_index("ix_data_packages_task_created", "data_packages", ["task_id", "created_at"])

    op.create_table(
        "data_package_members",
        sa.Column("member_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("package_id", sa.String(length=80), nullable=False),
        sa.Column("member_name", sa.String(length=120), nullable=False),
        sa.Column("member_kind", sa.String(length=32), nullable=False),
        sa.Column("artifact_id", sa.String(length=80), nullable=False),
        sa.Column("artifact_version_id", sa.String(length=80), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_data_package_members_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "member_kind IN ('source', 'manifest', 'payload', 'description')",
            name=op.f("ck_data_package_members_member_kind_valid"),
        ),
        sa.CheckConstraint(
            "row_count IS NULL OR row_count >= 0",
            name=op.f("ck_data_package_members_row_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "member_kind = 'payload' OR row_count IS NULL",
            name=op.f("ck_data_package_members_row_count_payload_only"),
        ),
        sa.CheckConstraint(
            "(member_name = 'manifest.json' AND member_kind = 'manifest' "
            "AND media_type = 'application/json' AND row_count IS NULL) OR "
            "(member_name = 'source/original.dxf' AND member_kind = 'source' "
            "AND media_type = 'application/dxf' AND row_count IS NULL) OR "
            "(member_name = 'description.md' AND member_kind = 'description' "
            "AND media_type = 'text/markdown' AND row_count IS NULL) OR "
            "(member_name IN ('payloads/records/part-00000.parquet', "
            "'payloads/raw_tags/part-00000.parquet', "
            "'payloads/entities/part-00000.parquet', "
            "'payloads/geometry_elements/part-00000.parquet', "
            "'payloads/xdata_tags/part-00000.parquet', "
            "'payloads/relationships/part-00000.parquet') AND member_kind = 'payload' "
            "AND media_type = 'application/vnd.apache.parquet' AND row_count IS NOT NULL)",
            name=op.f("ck_data_package_members_source_l0_member_contract_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_data_package_members_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "package_id"],
            ["data_packages.owner_id", "data_packages.task_id", "data_packages.package_id"],
            name="fk_data_package_members_owner_task_package",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "artifact_id", "artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_data_package_members_exact_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("member_id", name=op.f("pk_data_package_members")),
        sa.UniqueConstraint(
            "package_id", "member_name", name="uq_data_package_members_member_name"
        ),
    )
    op.create_index(
        op.f("ix_data_package_members_tenant_id"), "data_package_members", ["tenant_id"]
    )

    op.create_table(
        "critical_operations",
        sa.Column("operation_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("tool_name", sa.String(length=120), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_package_id", sa.String(length=80), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_critical_operations_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')", name=op.f("ck_critical_operations_status_valid")
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND error_code IS NULL AND error_message IS NULL) OR (status = 'failed' AND error_code IS NOT NULL AND result_package_id IS NULL)",
            name=op.f("ck_critical_operations_terminal_result_valid"),
        ),
        sa.CheckConstraint(
            "finished_at >= started_at", name=op.f("ck_critical_operations_time_order_valid")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_critical_operations_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_critical_operations_owner_task_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "result_package_id", "request_fingerprint"],
            [
                "data_packages.owner_id",
                "data_packages.task_id",
                "data_packages.package_id",
                "data_packages.request_fingerprint",
            ],
            name="fk_critical_operations_result_package",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("pk_critical_operations")),
        sa.UniqueConstraint(
            "owner_id", "task_id", "operation_id", name="uq_critical_operations_scope"
        ),
        sa.UniqueConstraint(
            "run_id", "idempotency_key", name="uq_critical_operations_run_idempotency"
        ),
        sa.UniqueConstraint(
            "run_id",
            "tool_name",
            "request_fingerprint",
            name="uq_critical_operations_run_tool_fingerprint",
        ),
    )
    op.create_index(op.f("ix_critical_operations_tenant_id"), "critical_operations", ["tenant_id"])
    op.create_index(
        "ix_critical_operations_run_created", "critical_operations", ["run_id", "created_at"]
    )

    op.create_table(
        "data_catalog_entries",
        sa.Column("catalog_entry_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("resource_kind", sa.String(length=32), nullable=False),
        sa.Column("alias", sa.String(length=32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("catalog_revision", sa.Integer(), nullable=False),
        sa.Column("package_id", sa.String(length=80), nullable=True),
        sa.Column("operation_id", sa.String(length=80), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_data_catalog_entries_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "resource_kind IN ('package', 'operation')",
            name=op.f("ck_data_catalog_entries_resource_kind_valid"),
        ),
        sa.CheckConstraint("ordinal > 0", name=op.f("ck_data_catalog_entries_ordinal_positive")),
        sa.CheckConstraint(
            "catalog_revision > 0",
            name=op.f("ck_data_catalog_entries_catalog_revision_positive"),
        ),
        sa.CheckConstraint(
            "(resource_kind = 'package' AND package_id IS NOT NULL AND operation_id IS NULL) OR (resource_kind = 'operation' AND operation_id IS NOT NULL AND package_id IS NULL)",
            name=op.f("ck_data_catalog_entries_resource_target_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_data_catalog_entries_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_data_catalog_entries_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "package_id"],
            ["data_packages.owner_id", "data_packages.task_id", "data_packages.package_id"],
            name="fk_data_catalog_entries_package",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "operation_id"],
            [
                "critical_operations.owner_id",
                "critical_operations.task_id",
                "critical_operations.operation_id",
            ],
            name="fk_data_catalog_entries_operation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("catalog_entry_id", name=op.f("pk_data_catalog_entries")),
        sa.UniqueConstraint("run_id", "alias", name="uq_data_catalog_entries_run_alias"),
        sa.UniqueConstraint(
            "run_id", "resource_kind", "ordinal", name="uq_data_catalog_entries_run_ordinal"
        ),
        sa.UniqueConstraint("run_id", "package_id", name="uq_data_catalog_entries_run_package"),
        sa.UniqueConstraint("run_id", "operation_id", name="uq_data_catalog_entries_run_operation"),
    )
    op.create_index(
        op.f("ix_data_catalog_entries_tenant_id"), "data_catalog_entries", ["tenant_id"]
    )
    op.create_index(
        "ix_data_catalog_entries_run_revision",
        "data_catalog_entries",
        ["run_id", "catalog_revision", "created_at"],
    )

    for table in DATA_TABLES:
        _tenant_table(table)
    tables = ", ".join(DATA_TABLES)
    for role in ("cad2bim_api_runtime", "cad2bim_runner_runtime", "cad2bim_mcp_runtime"):
        op.execute(sa.text(f"GRANT SELECT ON TABLE {tables} TO {role}"))
    publish_tables = ", ".join(DATA_TABLES[1:])
    for role in ("cad2bim_api_runtime", "cad2bim_runner_runtime", "cad2bim_mcp_runtime"):
        op.execute(sa.text(f"GRANT INSERT ON TABLE {publish_tables} TO {role}"))
    op.execute(sa.text("GRANT INSERT ON TABLE direct_session_bindings TO cad2bim_mcp_runtime"))
    op.execute(
        sa.text(
            "GRANT SELECT ON TABLE tenant_principals, users, task_tenant_bindings, "
            "tasks, runs, artifacts, artifact_versions TO cad2bim_mcp_runtime"
        )
    )
    op.execute(
        sa.text(
            "GRANT INSERT ON TABLE task_tenant_bindings, tasks, runs, artifacts, "
            "artifact_versions TO cad2bim_mcp_runtime"
        )
    )
    op.execute(sa.text("GRANT UPDATE ON TABLE tasks, runs, artifacts TO cad2bim_mcp_runtime"))
    op.execute(sa.text("GRANT SELECT ON TABLE alembic_version TO cad2bim_mcp_runtime"))
    op.execute(
        sa.text(
            f"GRANT EXECUTE ON FUNCTION {schema}.cad2bim_resolve_identity(text,text,text,text) "
            "TO cad2bim_mcp_runtime"
        )
    )
    _replace_claim_function(schema, filter_graph_mode=True)


def downgrade() -> None:
    schema = _quoted_schema()
    _replace_claim_function(schema, filter_graph_mode=False)
    op.execute(
        sa.text(
            f"REVOKE EXECUTE ON FUNCTION {schema}.cad2bim_resolve_identity(text,text,text,text) "
            "FROM cad2bim_mcp_runtime"
        )
    )
    op.execute(
        sa.text(
            "REVOKE SELECT ON TABLE tenant_principals, users, task_tenant_bindings, "
            "tasks, runs, artifacts, artifact_versions FROM cad2bim_mcp_runtime"
        )
    )
    op.execute(
        sa.text(
            "REVOKE INSERT ON TABLE task_tenant_bindings, tasks, runs, artifacts, "
            "artifact_versions FROM cad2bim_mcp_runtime"
        )
    )
    op.execute(sa.text("REVOKE UPDATE ON TABLE tasks, runs, artifacts FROM cad2bim_mcp_runtime"))
    op.execute(sa.text("REVOKE SELECT ON TABLE alembic_version FROM cad2bim_mcp_runtime"))
    for table in reversed(DATA_TABLES):
        op.execute(sa.text(f"DROP POLICY tenant_isolation ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
        op.drop_table(table)

    # 0058 cannot represent Direct runs: its protocol constraint requires every
    # Run to be pinned or quarantined.  Remove only the Task/Run/Artifact rows
    # created by this feature before restoring that constraint.  Immutable
    # storage bytes may remain orphaned for the normal storage GC to reclaim.
    op.execute(
        sa.text(
            "CREATE TEMP TABLE cad2bim_0059_direct_tasks ON COMMIT DROP AS "
            "SELECT DISTINCT tenant_id, task_id FROM runs "
            "WHERE execution_mode = 'direct_mcp'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE artifacts AS artifact SET current_version_id = NULL "
            "FROM runs AS run "
            "WHERE artifact.owner_id = run.owner_id "
            "AND artifact.task_id = run.task_id "
            "AND artifact.run_id = run.run_id "
            "AND run.execution_mode = 'direct_mcp'"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM artifact_versions AS version USING artifacts AS artifact, runs AS run "
            "WHERE version.owner_id = artifact.owner_id "
            "AND version.task_id = artifact.task_id "
            "AND version.artifact_id = artifact.artifact_id "
            "AND artifact.owner_id = run.owner_id "
            "AND artifact.task_id = run.task_id "
            "AND artifact.run_id = run.run_id "
            "AND run.execution_mode = 'direct_mcp'"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM artifacts AS artifact USING runs AS run "
            "WHERE artifact.owner_id = run.owner_id "
            "AND artifact.task_id = run.task_id "
            "AND artifact.run_id = run.run_id "
            "AND run.execution_mode = 'direct_mcp'"
        )
    )
    op.execute(sa.text("DELETE FROM runs WHERE execution_mode = 'direct_mcp'"))
    op.execute(
        sa.text(
            "DELETE FROM tasks AS task USING cad2bim_0059_direct_tasks AS direct_task "
            "WHERE task.tenant_id = direct_task.tenant_id "
            "AND task.task_id = direct_task.task_id"
        )
    )
    op.drop_index("ix_runs_claim_queue", table_name="runs")
    op.create_index(
        "ix_runs_claim_queue",
        "runs",
        ("status", "claim_not_before", "lease_expires_at", "created_at"),
    )
    op.drop_constraint(
        op.f("ck_runs_direct_run_not_runner_claimable"),
        "runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_runs_execution_protocol_mapping_valid"),
        "runs",
        type_="check",
    )
    op.create_check_constraint(
        # Recreate the exact 0058 name, including its historical double prefix.
        op.f("ck_runs_ck_runs_execution_protocol_mapping_valid"),
        "runs",
        "(execution_protocol_status = 'pinned' AND execution_protocol IS NOT NULL "
        "AND execution_protocol_id IS NOT NULL AND execution_protocol_digest IS NOT NULL "
        "AND execution_protocol_quarantine_reason IS NULL) OR "
        "(execution_protocol_status = 'quarantined' AND execution_protocol IS NULL "
        "AND execution_protocol_id IS NULL AND execution_protocol_digest IS NULL "
        "AND execution_protocol_quarantine_reason IS NOT NULL)",
    )
    op.drop_column("runs", "execution_mode")
