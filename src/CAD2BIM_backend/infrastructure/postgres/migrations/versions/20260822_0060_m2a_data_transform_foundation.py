"""Generalize immutable Data Runtime persistence for M2a transforms.

Revision ID: 20260822_0060
Revises: 20260821_0059
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260822_0060"
down_revision: str | None = "20260821_0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RESOURCE_TABLES = (
    "data_packages",
    "data_package_members",
    "critical_operations",
    "critical_operation_inputs",
    "data_catalog_entries",
)


def _json_type() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _replace_policy(table: str, *, task_scoped: bool) -> None:
    tenant = "tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '')"
    expression = tenant
    if task_scoped:
        expression += " AND task_id = NULLIF(current_setting('cad2bim.task_id', true), '')"
    op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({expression}) WITH CHECK ({expression})"
        )
    )


def upgrade() -> None:
    op.add_column("data_packages", sa.Column("description_title", sa.String(300)))
    op.add_column(
        "data_packages",
        sa.Column(
            "section_titles_json",
            _json_type(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE data_packages SET description_title = name, "
            "section_titles_json = "
            "'[\"Summary\", \"Intended use\", \"Contents\", \"Method\", "
            "\"Scope and completeness\", \"Known limitations\"]'::jsonb "
            "WHERE package_kind = 'source_l0'"
        )
    )
    op.alter_column("data_packages", "description_title", nullable=False)
    op.alter_column("data_packages", "source_filename", nullable=True)
    op.drop_constraint(
        op.f("ck_data_packages_package_kind_valid"),
        "data_packages",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_data_packages_package_kind_valid"),
        "data_packages",
        "package_kind IN ('source_l0', 'derived')",
    )
    op.create_check_constraint(
        op.f("ck_data_packages_source_filename_kind_valid"),
        "data_packages",
        "(package_kind = 'source_l0' AND source_filename IS NOT NULL) OR "
        "(package_kind = 'derived' AND source_filename IS NULL)",
    )
    op.alter_column("data_packages", "section_titles_json", server_default=None)

    op.alter_column(
        "data_package_members",
        "member_name",
        existing_type=sa.String(120),
        type_=sa.String(255),
        existing_nullable=False,
    )
    op.add_column(
        "data_package_members", sa.Column("logical_payload_name", sa.String(120))
    )
    op.add_column(
        "data_package_members", sa.Column("payload_description", sa.String(500))
    )
    op.add_column(
        "data_package_members", sa.Column("logical_key_fields_json", _json_type())
    )
    op.add_column(
        "data_package_members", sa.Column("arrow_schema_json", _json_type())
    )
    op.add_column(
        "data_package_members", sa.Column("logical_byte_count", sa.BigInteger())
    )
    op.drop_constraint(
        op.f("ck_data_package_members_member_kind_valid"),
        "data_package_members",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_data_package_members_source_l0_member_contract_valid"),
        "data_package_members",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_data_package_members_member_kind_valid"),
        "data_package_members",
        "member_kind IN ('source', 'manifest', 'payload', 'description', 'code')",
    )
    op.create_check_constraint(
        op.f("ck_data_package_members_member_contract_valid"),
        "data_package_members",
        "(member_name = 'manifest.json' AND member_kind = 'manifest' "
        "AND media_type = 'application/json' AND row_count IS NULL) OR "
        "(member_name = 'source/original.dxf' AND member_kind = 'source' "
        "AND media_type = 'application/dxf' AND row_count IS NULL) OR "
        "(member_name = 'description.md' AND member_kind = 'description' "
        "AND media_type = 'text/markdown' AND row_count IS NULL) OR "
        "(member_name = 'code/transformation.sql' AND member_kind = 'code' "
        "AND media_type = 'application/sql' AND row_count IS NULL) OR "
        "(member_kind = 'payload' AND media_type = 'application/vnd.apache.parquet' "
        "AND row_count IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_data_package_members_payload_metadata_scope_valid"),
        "data_package_members",
        "member_kind = 'payload' OR (logical_payload_name IS NULL "
        "AND payload_description IS NULL AND logical_key_fields_json IS NULL "
        "AND arrow_schema_json IS NULL AND logical_byte_count IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_data_package_members_logical_byte_count_nonnegative"),
        "data_package_members",
        "logical_byte_count IS NULL OR logical_byte_count >= 0",
    )
    op.create_unique_constraint(
        "uq_data_package_members_scope_member",
        "data_package_members",
        ("owner_id", "task_id", "package_id", "member_id"),
    )

    op.add_column(
        "critical_operations",
        sa.Column(
            "operation_kind",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'source_ingestion'"),
        ),
    )
    op.add_column(
        "critical_operations",
        sa.Column(
            "initiator",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'direct_client'"),
        ),
    )
    op.add_column("critical_operations", sa.Column("intent_summary", sa.String(1303)))
    op.add_column("critical_operations", sa.Column("error_phase", sa.String(32)))
    op.add_column("critical_operations", sa.Column("code_artifact_id", sa.String(80)))
    op.add_column(
        "critical_operations", sa.Column("code_artifact_version_id", sa.String(80))
    )
    op.add_column(
        "critical_operations", sa.Column("code_artifact_sha256", sa.String(64))
    )
    op.add_column(
        "critical_operations",
        sa.Column("operation_detail_json", _json_type(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE critical_operations AS operation SET "
            "intent_summary = 'Import ' || operation.source_filename || "
            "' as an immutable CAD L0 source package.', "
            "error_phase = CASE WHEN operation.status = 'failed' "
            "THEN 'source_extraction' ELSE NULL END, "
            "operation_detail_json = jsonb_build_object("
            "'detail_kind', 'source_ingestion', "
            "'filename', operation.source_filename, "
            "'media_type', 'application/dxf', "
            "'size_bytes', source_version.size_bytes) "
            "FROM data_packages AS package "
            "JOIN data_package_members AS source_member "
            "ON source_member.owner_id = package.owner_id "
            "AND source_member.task_id = package.task_id "
            "AND source_member.package_id = package.package_id "
            "AND source_member.member_name = 'source/original.dxf' "
            "AND source_member.member_kind = 'source' "
            "JOIN artifact_versions AS source_version "
            "ON source_version.owner_id = source_member.owner_id "
            "AND source_version.task_id = source_member.task_id "
            "AND source_version.artifact_id = source_member.artifact_id "
            "AND source_version.version_id = source_member.artifact_version_id "
            "AND source_version.sha256 = source_member.artifact_sha256 "
            "WHERE package.owner_id = operation.owner_id "
            "AND package.task_id = operation.task_id "
            "AND package.package_id = operation.result_package_id "
            "AND source_version.size_bytes > 0 "
            "AND operation.status = 'succeeded'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE critical_operations AS operation SET "
            "intent_summary = COALESCE(operation.intent_summary, "
            "'Import ' || operation.source_filename || "
            "' as an immutable CAD L0 source package.'), "
            "error_phase = CASE WHEN operation.status = 'failed' "
            "THEN 'source_extraction' ELSE operation.error_phase END, "
            "operation_detail_json = COALESCE(operation.operation_detail_json, "
            "jsonb_build_object('detail_kind', 'source_ingestion', "
            "'filename', operation.source_filename, "
            "'media_type', 'application/dxf', 'size_bytes', NULL)) "
            "WHERE operation.status = 'failed'"
        )
    )
    op.alter_column("critical_operations", "operation_detail_json", nullable=False)
    op.alter_column("critical_operations", "intent_summary", nullable=False)
    op.alter_column("critical_operations", "source_filename", nullable=True)
    op.alter_column("critical_operations", "operation_kind", server_default=None)
    op.alter_column("critical_operations", "initiator", server_default=None)
    op.drop_constraint(
        op.f("ck_critical_operations_terminal_result_valid"),
        "critical_operations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_critical_operations_operation_kind_valid"),
        "critical_operations",
        "operation_kind IN ('source_ingestion', 'data_transform')",
    )
    op.create_check_constraint(
        op.f("ck_critical_operations_initiator_valid"),
        "critical_operations",
        "initiator = 'direct_client'",
    )
    op.create_check_constraint(
        op.f("ck_critical_operations_operation_kind_detail_valid"),
        "critical_operations",
        "(operation_kind = 'source_ingestion' AND tool_name = 'cad_import_source' "
        "AND source_filename IS NOT NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NULL "
        "AND code_artifact_version_id IS NULL AND code_artifact_sha256 IS NULL) OR "
        "(operation_kind = 'data_transform' AND tool_name = 'data_transform' "
        "AND source_filename IS NULL AND intent_summary IS NOT NULL "
        "AND code_artifact_id IS NOT NULL AND code_artifact_version_id IS NOT NULL "
        "AND code_artifact_sha256 IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_critical_operations_terminal_result_valid"),
        "critical_operations",
        "(status = 'succeeded' AND result_package_id IS NOT NULL "
        "AND error_code IS NULL AND error_message IS NULL AND error_phase IS NULL) OR "
        "(status = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL "
        "AND result_package_id IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_critical_operations_error_phase_valid"),
        "critical_operations",
        "error_phase IS NULL OR "
        "(operation_kind = 'source_ingestion' AND status = 'failed' "
        "AND error_phase = 'source_extraction') OR "
        "(operation_kind = 'data_transform' AND status = 'failed' "
        "AND error_phase IN ('execution', 'output_validation'))",
    )
    op.create_foreign_key(
        "fk_critical_operations_exact_code_artifact_version",
        "critical_operations",
        "artifact_versions",
        ("owner_id", "task_id", "code_artifact_id", "code_artifact_version_id"),
        ("owner_id", "task_id", "artifact_id", "version_id"),
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_critical_operations_result_package",
        "critical_operations",
        ("result_package_id",),
    )

    op.create_table(
        "critical_operation_inputs",
        sa.Column("input_binding_id", sa.String(80), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("operation_id", sa.String(80), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("input_name", sa.String(63), nullable=False),
        sa.Column("package_id", sa.String(80), nullable=False),
        sa.Column("member_id", sa.String(80), nullable=False),
        sa.Column("payload_name", sa.String(120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ("tenant_id",),
            ("tenants.tenant_id",),
            name=op.f("fk_critical_operation_inputs_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("tenant_id", "task_id"),
            ("tasks.tenant_id", "tasks.task_id"),
            name="fk_critical_operation_inputs_tenant_task_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("owner_id", "task_id", "operation_id"),
            ("critical_operations.owner_id", "critical_operations.task_id", "critical_operations.operation_id"),
            name="fk_critical_operation_inputs_operation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("owner_id", "task_id", "package_id", "member_id"),
            (
                "data_package_members.owner_id",
                "data_package_members.task_id",
                "data_package_members.package_id",
                "data_package_members.member_id",
            ),
            name="fk_critical_operation_inputs_exact_member",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "ordinal > 0 AND ordinal <= 16",
            name=op.f("ck_critical_operation_inputs_ordinal_valid"),
        ),
        sa.PrimaryKeyConstraint(
            "input_binding_id", name=op.f("pk_critical_operation_inputs")
        ),
        sa.UniqueConstraint(
            "operation_id", "ordinal", name="uq_critical_operation_inputs_ordinal"
        ),
        sa.UniqueConstraint(
            "operation_id", "input_name", name="uq_critical_operation_inputs_name"
        ),
        sa.UniqueConstraint(
            "operation_id",
            "package_id",
            "member_id",
            name="uq_critical_operation_inputs_member",
        ),
    )
    op.create_index(
        op.f("ix_critical_operation_inputs_tenant_id"),
        "critical_operation_inputs",
        ("tenant_id",),
    )

    op.alter_column(
        "data_catalog_entries",
        "ordinal",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )
    op.alter_column(
        "data_catalog_entries",
        "catalog_revision",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )
    op.create_index(
        "ix_data_catalog_entries_run_kind_ordinal",
        "data_catalog_entries",
        ("run_id", "resource_kind", "ordinal"),
    )

    for table in RESOURCE_TABLES:
        _replace_policy(table, task_scoped=True)
    for role in ("cad2bim_api_runtime", "cad2bim_runner_runtime", "cad2bim_mcp_runtime"):
        op.execute(sa.text(f"GRANT SELECT, INSERT ON critical_operation_inputs TO {role}"))


def downgrade() -> None:
    for role in ("cad2bim_api_runtime", "cad2bim_runner_runtime", "cad2bim_mcp_runtime"):
        op.execute(sa.text(f"REVOKE SELECT, INSERT ON critical_operation_inputs FROM {role}"))

    # 0059 cannot represent derived packages or transform operations.
    op.execute(
        sa.text(
            "DELETE FROM data_catalog_entries WHERE operation_id IN "
            "(SELECT operation_id FROM critical_operations "
            "WHERE operation_kind = 'data_transform') OR package_id IN "
            "(SELECT package_id FROM data_packages WHERE package_kind = 'derived')"
        )
    )
    op.drop_table("critical_operation_inputs")
    op.execute(
        sa.text("DELETE FROM critical_operations WHERE operation_kind = 'data_transform'")
    )
    op.execute(
        sa.text(
            "DELETE FROM data_package_members WHERE package_id IN "
            "(SELECT package_id FROM data_packages WHERE package_kind = 'derived')"
        )
    )
    op.execute(sa.text("DELETE FROM data_packages WHERE package_kind = 'derived'"))

    for table in (
        "data_packages",
        "data_package_members",
        "critical_operations",
        "data_catalog_entries",
    ):
        _replace_policy(table, task_scoped=False)

    op.drop_index(
        "ix_data_catalog_entries_run_kind_ordinal", table_name="data_catalog_entries"
    )
    op.alter_column(
        "data_catalog_entries",
        "catalog_revision",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        "data_catalog_entries",
        "ordinal",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )

    op.drop_constraint(
        "uq_critical_operations_result_package", "critical_operations", type_="unique"
    )
    op.drop_constraint(
        "fk_critical_operations_exact_code_artifact_version",
        "critical_operations",
        type_="foreignkey",
    )
    for name in (
        "error_phase_valid",
        "operation_kind_detail_valid",
        "initiator_valid",
        "operation_kind_valid",
    ):
        op.drop_constraint(
            op.f(f"ck_critical_operations_{name}"),
            "critical_operations",
            type_="check",
        )
    op.drop_constraint(
        op.f("ck_critical_operations_terminal_result_valid"),
        "critical_operations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_critical_operations_terminal_result_valid"),
        "critical_operations",
        "(status = 'succeeded' AND error_code IS NULL AND error_message IS NULL) OR "
        "(status = 'failed' AND error_code IS NOT NULL AND result_package_id IS NULL)",
    )
    op.alter_column("critical_operations", "source_filename", nullable=False)
    op.alter_column("critical_operations", "intent_summary", nullable=True)
    for column in (
        "operation_detail_json",
        "code_artifact_sha256",
        "code_artifact_version_id",
        "code_artifact_id",
        "error_phase",
        "intent_summary",
        "initiator",
        "operation_kind",
    ):
        op.drop_column("critical_operations", column)

    op.drop_constraint(
        "uq_data_package_members_scope_member", "data_package_members", type_="unique"
    )
    for name in (
        "logical_byte_count_nonnegative",
        "payload_metadata_scope_valid",
        "member_contract_valid",
    ):
        op.drop_constraint(
            op.f(f"ck_data_package_members_{name}"),
            "data_package_members",
            type_="check",
        )
    op.drop_constraint(
        op.f("ck_data_package_members_member_kind_valid"),
        "data_package_members",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_data_package_members_member_kind_valid"),
        "data_package_members",
        "member_kind IN ('source', 'manifest', 'payload', 'description')",
    )
    op.create_check_constraint(
        op.f("ck_data_package_members_source_l0_member_contract_valid"),
        "data_package_members",
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
        "'payloads/relationships/part-00000.parquet') "
        "AND member_kind = 'payload' "
        "AND media_type = 'application/vnd.apache.parquet' AND row_count IS NOT NULL)",
    )
    for column in (
        "logical_byte_count",
        "arrow_schema_json",
        "logical_key_fields_json",
        "payload_description",
        "logical_payload_name",
    ):
        op.drop_column("data_package_members", column)
    op.alter_column(
        "data_package_members",
        "member_name",
        existing_type=sa.String(255),
        type_=sa.String(120),
        existing_nullable=False,
    )

    op.drop_constraint(
        op.f("ck_data_packages_source_filename_kind_valid"),
        "data_packages",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_data_packages_package_kind_valid"),
        "data_packages",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_data_packages_package_kind_valid"),
        "data_packages",
        "package_kind = 'source_l0'",
    )
    op.alter_column("data_packages", "source_filename", nullable=False)
    op.drop_column("data_packages", "section_titles_json")
    op.drop_column("data_packages", "description_title")
