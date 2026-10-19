"""Add durable geometry resource roots, exact versions, and asset bindings.

Revision ID: 20260902_0066
Revises: 20260902_0065
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260902_0066"
down_revision: str | None = "20260902_0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_GEOMETRY_TABLES = (
    "geometry_resources",
    "geometry_resource_versions",
    "geometry_resource_version_assets",
)
_RUNTIME_READ_ROLES = (
    "cad2bim_api_runtime",
    "cad2bim_runner_runtime",
    "cad2bim_mcp_runtime",
)
_RUNTIME_WRITE_ROLES = (
    "cad2bim_runner_runtime",
    "cad2bim_mcp_runtime",
)
_SYSTEM_CURATOR_ROLE = "cad2bim_system_curator"


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _lower_hex_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) = 64 AND {column_name} = lower({column_name}) "
        f"AND {remainder} = ''"
    )


def _enable_rls() -> None:
    for table in _GEOMETRY_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))

    runtime_roles = ", ".join(_RUNTIME_READ_ROLES)
    runtime_write_roles = ", ".join(_RUNTIME_WRITE_ROLES)
    runtime_root_visibility = (
        "owner_scope = 'SYSTEM' OR "
        "(owner_scope = 'TENANT' AND "
        "owner_tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '')) OR "
        "(owner_scope = 'TASK' AND "
        "owner_tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '') AND "
        "owner_task_id = NULLIF(current_setting('cad2bim.task_id', true), ''))"
    )
    runtime_task_write = (
        "owner_scope = 'TASK' AND "
        "owner_tenant_id = NULLIF(current_setting('cad2bim.tenant_id', true), '') AND "
        "owner_task_id = NULLIF(current_setting('cad2bim.task_id', true), '')"
    )
    runtime_version_visibility = (
        "EXISTS (SELECT 1 FROM geometry_resources AS resource "
        "WHERE resource.resource_id = geometry_resource_versions.resource_id)"
    )
    runtime_version_write = (
        "EXISTS (SELECT 1 FROM geometry_resources AS resource "
        "WHERE resource.resource_id = geometry_resource_versions.resource_id "
        "AND resource.owner_scope = 'TASK' AND "
        "resource.owner_tenant_id = "
        "NULLIF(current_setting('cad2bim.tenant_id', true), '') AND "
        "resource.owner_task_id = "
        "NULLIF(current_setting('cad2bim.task_id', true), ''))"
    )
    system_version_write = (
        "EXISTS (SELECT 1 FROM geometry_resources AS resource "
        "WHERE resource.resource_id = geometry_resource_versions.resource_id "
        "AND resource.owner_scope = 'SYSTEM')"
    )
    runtime_asset_visibility = (
        "EXISTS (SELECT 1 FROM geometry_resource_versions AS version "
        "JOIN geometry_resources AS resource "
        "ON resource.resource_id = version.resource_id "
        "WHERE version.resource_version_id = "
        "geometry_resource_version_assets.resource_version_id)"
    )
    runtime_asset_write = (
        "EXISTS (SELECT 1 FROM geometry_resource_versions AS version "
        "JOIN geometry_resources AS resource "
        "ON resource.resource_id = version.resource_id "
        "WHERE version.resource_version_id = "
        "geometry_resource_version_assets.resource_version_id "
        "AND resource.owner_scope = 'TASK' AND "
        "resource.owner_tenant_id = "
        "NULLIF(current_setting('cad2bim.tenant_id', true), '') AND "
        "resource.owner_task_id = "
        "NULLIF(current_setting('cad2bim.task_id', true), ''))"
    )
    system_asset_write = (
        "EXISTS (SELECT 1 FROM geometry_resource_versions AS version "
        "JOIN geometry_resources AS resource "
        "ON resource.resource_id = version.resource_id "
        "WHERE version.resource_version_id = "
        "geometry_resource_version_assets.resource_version_id "
        "AND resource.owner_scope = 'SYSTEM')"
    )

    op.execute(
        sa.text(
            "CREATE POLICY geometry_resources_runtime_select ON geometry_resources "
            f"FOR SELECT TO {runtime_roles} USING ({runtime_root_visibility})"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY geometry_resources_runtime_insert ON geometry_resources "
            f"FOR INSERT TO {runtime_write_roles} WITH CHECK ({runtime_task_write})"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY geometry_resources_system_select ON geometry_resources "
            f"FOR SELECT TO {_SYSTEM_CURATOR_ROLE} USING (owner_scope = 'SYSTEM')"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY geometry_resources_system_insert ON geometry_resources "
            f"FOR INSERT TO {_SYSTEM_CURATOR_ROLE} "
            "WITH CHECK (owner_scope = 'SYSTEM')"
        )
    )

    for table, visibility, runtime_write, system_write in (
        (
            "geometry_resource_versions",
            runtime_version_visibility,
            runtime_version_write,
            system_version_write,
        ),
        (
            "geometry_resource_version_assets",
            runtime_asset_visibility,
            runtime_asset_write,
            system_asset_write,
        ),
    ):
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_runtime_select ON {table} "
                f"FOR SELECT TO {runtime_roles} USING ({visibility})"
            )
        )
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_runtime_insert ON {table} "
                f"FOR INSERT TO {runtime_write_roles} WITH CHECK ({runtime_write})"
            )
        )
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_system_select ON {table} "
                f"FOR SELECT TO {_SYSTEM_CURATOR_ROLE} USING ({visibility})"
            )
        )
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_system_insert ON {table} "
                f"FOR INSERT TO {_SYSTEM_CURATOR_ROLE} WITH CHECK ({system_write})"
            )
        )


def _grant_minimum_privileges() -> None:
    tables = ", ".join(_GEOMETRY_TABLES)
    for role in _RUNTIME_READ_ROLES:
        op.execute(sa.text(f"GRANT SELECT ON TABLE {tables} TO {role}"))
    for role in _RUNTIME_WRITE_ROLES:
        op.execute(sa.text(f"GRANT INSERT ON TABLE {tables} TO {role}"))
    op.execute(
        sa.text(
            f"GRANT SELECT, INSERT ON TABLE {tables} TO {_SYSTEM_CURATOR_ROLE}"
        )
    )


def upgrade() -> None:
    op.create_table(
        "geometry_resources",
        sa.Column("resource_id", sa.String(length=80), nullable=False),
        sa.Column("resource_kind", sa.String(length=32), nullable=False),
        sa.Column("owner_scope", sa.String(length=16), nullable=False),
        sa.Column("owner_tenant_id", sa.String(length=80)),
        sa.Column("owner_task_id", sa.String(length=80)),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["owner_tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_geometry_resources_owner_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_tenant_id", "owner_task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_geometry_resources_owner_tenant_task_tasks",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "resource_kind IN ('mapped_model', 'modeling_function')",
            name=op.f("ck_geometry_resources_resource_kind_valid"),
        ),
        sa.CheckConstraint(
            "owner_scope IN ('SYSTEM', 'TENANT', 'TASK')",
            name=op.f("ck_geometry_resources_owner_scope_valid"),
        ),
        sa.CheckConstraint(
            "(owner_scope = 'SYSTEM' AND owner_tenant_id IS NULL "
            "AND owner_task_id IS NULL) OR "
            "(owner_scope = 'TENANT' AND owner_tenant_id IS NOT NULL "
            "AND owner_task_id IS NULL) OR "
            "(owner_scope = 'TASK' AND owner_tenant_id IS NOT NULL "
            "AND owner_task_id IS NOT NULL)",
            name=op.f("ck_geometry_resources_owner_identity_valid"),
        ),
        sa.CheckConstraint(
            "display_name <> '' AND display_name = trim(display_name)",
            name=op.f("ck_geometry_resources_display_name_valid"),
        ),
        sa.PrimaryKeyConstraint("resource_id", name=op.f("pk_geometry_resources")),
    )
    op.create_index(
        "ix_geometry_resources_scope_kind_created",
        "geometry_resources",
        (
            "owner_scope",
            "owner_tenant_id",
            "owner_task_id",
            "resource_kind",
            "created_at",
        ),
    )

    op.create_table(
        "geometry_resource_versions",
        sa.Column("resource_version_id", sa.String(length=80), nullable=False),
        sa.Column("resource_id", sa.String(length=80), nullable=False),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("origin_kind", sa.String(length=64), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest_json", postgresql.JSONB(none_as_null=True), nullable=False),
        sa.Column("lineage_json", postgresql.JSONB(none_as_null=True), nullable=False),
        sa.Column(
            "validation_summary_json",
            postgresql.JSONB(none_as_null=True),
            nullable=False,
        ),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["geometry_resources.resource_id"],
            name=op.f("fk_geometry_resource_versions_resource_id_geometry_resources"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "version_no > 0",
            name=op.f("ck_geometry_resource_versions_version_no_positive"),
        ),
        sa.CheckConstraint(
            "origin_kind <> '' AND origin_kind = trim(origin_kind)",
            name=op.f("ck_geometry_resource_versions_origin_kind_valid"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("content_digest"),
            name=op.f("ck_geometry_resource_versions_content_digest_shape"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("manifest_digest"),
            name=op.f("ck_geometry_resource_versions_manifest_digest_shape"),
        ),
        sa.PrimaryKeyConstraint(
            "resource_version_id",
            name=op.f("pk_geometry_resource_versions"),
        ),
        sa.UniqueConstraint(
            "resource_id",
            "version_no",
            name="uq_geometry_resource_versions_resource_version_no",
        ),
    )
    op.create_index(
        "ix_geometry_resource_versions_resource_created",
        "geometry_resource_versions",
        ("resource_id", "created_at"),
    )

    op.create_table(
        "geometry_resource_version_assets",
        sa.Column("resource_version_id", sa.String(length=80), nullable=False),
        sa.Column("asset_role", sa.String(length=64), nullable=False),
        sa.Column("asset_format", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["resource_version_id"],
            ["geometry_resource_versions.resource_version_id"],
            name="fk_geometry_resource_assets_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "asset_role IN ('canonical_geometry', 'manifest', 'validation_report', "
            "'source_bundle', 'runtime_manifest', 'parameter_schema', 'preview', "
            "'inspection', 'sample_output')",
            name=op.f("ck_geometry_resource_version_assets_asset_role_valid"),
        ),
        sa.CheckConstraint(
            "asset_format <> '' AND asset_format = trim(asset_format)",
            name=op.f("ck_geometry_resource_version_assets_asset_format_valid"),
        ),
        sa.CheckConstraint(
            "content_type <> '' AND content_type = trim(content_type)",
            name=op.f("ck_geometry_resource_version_assets_content_type_valid"),
        ),
        sa.CheckConstraint(
            "filename <> '' AND filename = trim(filename)",
            name=op.f("ck_geometry_resource_version_assets_filename_valid"),
        ),
        sa.CheckConstraint(
            "size_bytes > 0",
            name=op.f("ck_geometry_resource_version_assets_size_bytes_positive"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("sha256"),
            name=op.f("ck_geometry_resource_version_assets_sha256_shape"),
        ),
        sa.PrimaryKeyConstraint(
            "resource_version_id",
            "asset_role",
            name=op.f("pk_geometry_resource_version_assets"),
        ),
    )
    op.create_index(
        "ix_geometry_resource_version_assets_sha256",
        "geometry_resource_version_assets",
        ("sha256",),
    )

    _enable_rls()
    _grant_minimum_privileges()


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            LOCK TABLE
                geometry_resources,
                geometry_resource_versions,
                geometry_resource_version_assets
            IN ACCESS EXCLUSIVE MODE
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                resources_count bigint;
                versions_count bigint;
                assets_count bigint;
            BEGIN
                SELECT count(*) INTO resources_count FROM geometry_resources;
                SELECT count(*) INTO versions_count FROM geometry_resource_versions;
                SELECT count(*) INTO assets_count FROM geometry_resource_version_assets;
                IF resources_count <> 0 OR versions_count <> 0 OR assets_count <> 0 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = format(
                            'geometry catalog is not empty: geometry_resources=%s, '
                            'geometry_resource_versions=%s, '
                            'geometry_resource_version_assets=%s',
                            resources_count,
                            versions_count,
                            assets_count
                        );
                END IF;
            END
            $$
            """
        )
    )
    tables = ", ".join(_GEOMETRY_TABLES)
    for role in _RUNTIME_WRITE_ROLES:
        op.execute(sa.text(f"REVOKE INSERT ON TABLE {tables} FROM {role}"))
    for role in _RUNTIME_READ_ROLES:
        op.execute(sa.text(f"REVOKE SELECT ON TABLE {tables} FROM {role}"))
    op.execute(
        sa.text(
            f"REVOKE SELECT, INSERT ON TABLE {tables} FROM {_SYSTEM_CURATOR_ROLE}"
        )
    )
    op.drop_table("geometry_resource_version_assets")
    op.drop_table("geometry_resource_versions")
    op.drop_table("geometry_resources")
