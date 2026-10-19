"""Persist M2c Python/Arrow and exact JSON Package members.

Revision ID: 20260823_0061
Revises: 20260822_0060
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260823_0061"
down_revision: str | None = "20260822_0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("data_package_members", sa.Column("json_kind", sa.String(16)))
    for name in (
        "payload_metadata_scope_valid",
        "member_contract_valid",
        "member_kind_valid",
    ):
        op.drop_constraint(
            op.f(f"ck_data_package_members_{name}"),
            "data_package_members",
            type_="check",
        )
    op.create_check_constraint(
        op.f("ck_data_package_members_member_kind_valid"),
        "data_package_members",
        "member_kind IN ('source', 'manifest', 'payload', 'description', 'code', 'schema')",
    )
    op.create_check_constraint(
        op.f("ck_data_package_members_json_kind_valid"),
        "data_package_members",
        "json_kind IS NULL OR json_kind IN ('document', 'records')",
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
        "(member_name = 'code/transformation.py' AND member_kind = 'code' "
        "AND media_type = 'text/x-python' AND row_count IS NULL) OR "
        "(member_kind = 'payload' AND media_type = 'application/vnd.apache.parquet' "
        "AND row_count IS NOT NULL AND json_kind IS NULL) OR "
        "(member_kind = 'payload' AND media_type = 'application/json' "
        "AND member_name LIKE 'payloads/%.json' AND row_count IS NULL "
        "AND json_kind IN ('document', 'records')) OR "
        "(member_kind = 'schema' AND media_type = 'application/schema+json' "
        "AND member_name LIKE 'schemas/%.schema.json' AND row_count IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_data_package_members_payload_metadata_scope_valid"),
        "data_package_members",
        "member_kind = 'payload' OR (logical_payload_name IS NULL "
        "AND payload_description IS NULL AND logical_key_fields_json IS NULL "
        "AND arrow_schema_json IS NULL AND logical_byte_count IS NULL "
        "AND json_kind IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_data_package_members_payload_format_metadata_valid"),
        "data_package_members",
        "member_kind <> 'payload' OR "
        "(media_type = 'application/vnd.apache.parquet' AND row_count IS NOT NULL "
        "AND json_kind IS NULL) OR "
        "(media_type = 'application/json' AND row_count IS NULL "
        "AND json_kind IN ('document', 'records') "
        "AND logical_key_fields_json IS NULL AND arrow_schema_json IS NULL)",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM data_package_members
                    WHERE member_kind = 'schema' OR json_kind IS NOT NULL
                       OR member_name = 'code/transformation.py'
                ) OR EXISTS (
                    SELECT 1 FROM critical_operations
                    WHERE operation_detail_json ->> 'engine' = 'python_arrow'
                       OR COALESCE(jsonb_array_length(
                           operation_detail_json -> 'inline_tables'
                       ), 0) > 0
                ) THEN
                    RAISE EXCEPTION
                        'cannot downgrade 0061 while M2c package or operation facts exist';
                END IF;
            END
            $$
            """
        )
    )
    for name in (
        "payload_format_metadata_valid",
        "payload_metadata_scope_valid",
        "member_contract_valid",
        "json_kind_valid",
        "member_kind_valid",
    ):
        op.drop_constraint(
            op.f(f"ck_data_package_members_{name}"),
            "data_package_members",
            type_="check",
        )
    op.drop_column("data_package_members", "json_kind")
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
