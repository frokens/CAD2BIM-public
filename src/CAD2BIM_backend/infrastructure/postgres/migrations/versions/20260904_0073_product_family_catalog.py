"""Add the ready-only immutable ProductFamily catalog.

Revision ID: 20260904_0073
Revises: 20260904_0072
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260904_0073"
down_revision: str | None = "20260904_0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = (
    "product_families",
    "product_family_versions",
    "product_family_geometry_bindings",
    "product_family_version_assets",
)
_RUNTIME_READ_ROLES = (
    "cad2bim_api_runtime",
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
        f"length({column_name}) = 64 AND {column_name} = lower({column_name}) AND {remainder} = ''"
    )


def _lower_slug_check(column_name: str) -> str:
    remainder = column_name
    for character in "abcdefghijklmnopqrstuvwxyz0123456789_":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column_name}) BETWEEN 1 AND 64 "
        f"AND substr({column_name}, 1, 1) BETWEEN 'a' AND 'z' "
        f"AND {remainder} = ''"
    )


def _create_binding_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_guard_product_family_geometry_binding()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            DECLARE
                family_scope text;
                family_tenant_id text;
                family_task_id text;
                geometry_scope text;
                geometry_tenant_id text;
                geometry_task_id text;
                geometry_kind text;
                geometry_digest text;
            BEGIN
                SELECT
                    family.owner_scope,
                    family.owner_tenant_id,
                    family.owner_task_id,
                    geometry.owner_scope,
                    geometry.owner_tenant_id,
                    geometry.owner_task_id,
                    geometry.resource_kind,
                    geometry_version.content_digest
                INTO
                    family_scope,
                    family_tenant_id,
                    family_task_id,
                    geometry_scope,
                    geometry_tenant_id,
                    geometry_task_id,
                    geometry_kind,
                    geometry_digest
                FROM product_family_versions AS family_version
                JOIN product_families AS family
                  ON family.family_id = family_version.family_id
                JOIN geometry_resource_versions AS geometry_version
                  ON geometry_version.resource_id = NEW.geometry_resource_id
                 AND geometry_version.resource_version_id =
                     NEW.geometry_resource_version_id
                JOIN geometry_resources AS geometry
                  ON geometry.resource_id = geometry_version.resource_id
                WHERE family_version.family_version_id = NEW.family_version_id;

                IF NOT FOUND THEN
                    RAISE EXCEPTION 'ProductFamily binding does not resolve exact parents'
                        USING ERRCODE = '23503';
                END IF;
                IF geometry_kind <> NEW.expected_resource_kind
                   OR geometry_digest <> NEW.geometry_content_digest THEN
                    RAISE EXCEPTION 'ProductFamily binding geometry identity is inconsistent'
                        USING ERRCODE = '23514';
                END IF;
                IF (family_scope = 'SYSTEM' AND geometry_scope <> 'SYSTEM')
                   OR (family_scope = 'TENANT' AND NOT (
                        geometry_scope = 'SYSTEM'
                        OR (geometry_scope = 'TENANT'
                            AND geometry_tenant_id = family_tenant_id)
                   ))
                   OR (family_scope = 'TASK' AND NOT (
                        geometry_scope = 'SYSTEM'
                        OR (geometry_scope = 'TENANT'
                            AND geometry_tenant_id = family_tenant_id)
                        OR (geometry_scope = 'TASK'
                            AND geometry_tenant_id = family_tenant_id
                            AND geometry_task_id = family_task_id)
                   )) THEN
                    RAISE EXCEPTION 'ProductFamily dependency is narrower than its owner scope'
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
            "CREATE TRIGGER trg_product_family_geometry_binding_guard "
            "BEFORE INSERT OR UPDATE ON product_family_geometry_bindings "
            "FOR EACH ROW EXECUTE FUNCTION "
            "cad2bim_guard_product_family_geometry_binding()"
        )
    )


def _create_completeness_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION cad2bim_guard_product_family_version_complete()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $guard$
            DECLARE
                binding_count bigint;
                source_count bigint;
            BEGIN
                SELECT count(*) INTO binding_count
                FROM product_family_geometry_bindings
                WHERE family_version_id = NEW.family_version_id;
                SELECT count(*) INTO source_count
                FROM product_family_version_assets
                WHERE family_version_id = NEW.family_version_id
                  AND asset_role = 'realization_source_bundle';
                IF binding_count < 1 OR source_count <> 1 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = format(
                            'ProductFamily version is incomplete: bindings=%s, '
                            'realization_source_bundles=%s',
                            binding_count,
                            source_count
                        );
                END IF;
                RETURN NEW;
            END
            $guard$
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE CONSTRAINT TRIGGER trg_product_family_version_complete "
            "AFTER INSERT OR UPDATE ON product_family_versions "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
            "cad2bim_guard_product_family_version_complete()"
        )
    )


def _enable_rls_and_grant_reads() -> None:
    for table in _TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))

    runtime_roles = ", ".join(_RUNTIME_READ_ROLES)
    root_visibility = (
        "owner_scope = 'SYSTEM' OR "
        "(owner_scope = 'TENANT' AND owner_tenant_id = "
        "NULLIF(current_setting('cad2bim.tenant_id', true), '')) OR "
        "(owner_scope = 'TASK' AND owner_tenant_id = "
        "NULLIF(current_setting('cad2bim.tenant_id', true), '') AND "
        "owner_task_id = NULLIF(current_setting('cad2bim.task_id', true), ''))"
    )
    version_visibility = (
        "EXISTS (SELECT 1 FROM product_families AS family "
        "WHERE family.family_id = product_family_versions.family_id AND ("
        f"{root_visibility.replace('owner_', 'family.owner_')}))"
    )
    binding_visibility = (
        "EXISTS (SELECT 1 FROM product_family_versions AS family_version "
        "JOIN product_families AS family "
        "ON family.family_id = family_version.family_id "
        "WHERE family_version.family_version_id = "
        "product_family_geometry_bindings.family_version_id AND ("
        f"{root_visibility.replace('owner_', 'family.owner_')}))"
    )
    asset_visibility = (
        "EXISTS (SELECT 1 FROM product_family_versions AS family_version "
        "JOIN product_families AS family "
        "ON family.family_id = family_version.family_id "
        "WHERE family_version.family_version_id = "
        "product_family_version_assets.family_version_id AND ("
        f"{root_visibility.replace('owner_', 'family.owner_')}))"
    )
    system_version_visibility = (
        "EXISTS (SELECT 1 FROM product_families AS family "
        "WHERE family.family_id = product_family_versions.family_id "
        "AND family.owner_scope = 'SYSTEM')"
    )
    system_binding_visibility = (
        "EXISTS (SELECT 1 FROM product_family_versions AS family_version "
        "JOIN product_families AS family "
        "ON family.family_id = family_version.family_id "
        "WHERE family_version.family_version_id = "
        "product_family_geometry_bindings.family_version_id "
        "AND family.owner_scope = 'SYSTEM')"
    )
    system_asset_visibility = (
        "EXISTS (SELECT 1 FROM product_family_versions AS family_version "
        "JOIN product_families AS family "
        "ON family.family_id = family_version.family_id "
        "WHERE family_version.family_version_id = "
        "product_family_version_assets.family_version_id "
        "AND family.owner_scope = 'SYSTEM')"
    )

    for table, runtime_visibility, system_visibility in (
        ("product_families", root_visibility, "owner_scope = 'SYSTEM'"),
        ("product_family_versions", version_visibility, system_version_visibility),
        (
            "product_family_geometry_bindings",
            binding_visibility,
            system_binding_visibility,
        ),
        ("product_family_version_assets", asset_visibility, system_asset_visibility),
    ):
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_runtime_select ON {table} "
                f"FOR SELECT TO {runtime_roles} USING ({runtime_visibility})"
            )
        )
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_system_select ON {table} "
                f"FOR SELECT TO {_SYSTEM_CURATOR_ROLE} USING ({system_visibility})"
            )
        )

    tables = ", ".join(_TABLES)
    for role in _RUNTIME_READ_ROLES:
        op.execute(sa.text(f"GRANT SELECT ON TABLE {tables} TO {role}"))
    op.execute(sa.text(f"GRANT SELECT ON TABLE {tables} TO {_SYSTEM_CURATOR_ROLE}"))


def upgrade() -> None:
    op.create_table(
        "product_families",
        sa.Column("family_id", sa.String(length=80), nullable=False),
        sa.Column("owner_scope", sa.String(length=16), nullable=False),
        sa.Column("owner_tenant_id", sa.String(length=80)),
        sa.Column("owner_task_id", sa.String(length=80)),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["owner_tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_product_families_owner_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_tenant_id", "owner_task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_product_families_owner_tenant_task_tasks",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "owner_scope IN ('SYSTEM', 'TENANT', 'TASK')",
            name=op.f("ck_product_families_owner_scope_valid"),
        ),
        sa.CheckConstraint(
            "(owner_scope = 'SYSTEM' AND owner_tenant_id IS NULL "
            "AND owner_task_id IS NULL) OR "
            "(owner_scope = 'TENANT' AND owner_tenant_id IS NOT NULL "
            "AND owner_task_id IS NULL) OR "
            "(owner_scope = 'TASK' AND owner_tenant_id IS NOT NULL "
            "AND owner_task_id IS NOT NULL)",
            name=op.f("ck_product_families_owner_identity_valid"),
        ),
        sa.PrimaryKeyConstraint("family_id", name=op.f("pk_product_families")),
    )
    op.create_index(
        "ix_product_families_scope_created",
        "product_families",
        ("owner_scope", "owner_tenant_id", "owner_task_id", "created_at"),
    )

    op.create_table(
        "product_family_versions",
        sa.Column("family_version_id", sa.String(length=80), nullable=False),
        sa.Column("family_id", sa.String(length=80), nullable=False),
        sa.Column("version_no", sa.BigInteger(), nullable=False),
        sa.Column("parent_family_version_id", sa.String(length=80)),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.String(length=2048), nullable=False),
        sa.Column(
            "family_contract_json",
            postgresql.JSONB(none_as_null=True),
            nullable=False,
        ),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("program_entry", sa.String(length=64), nullable=False),
        sa.Column("runtime_profile", sa.String(length=64), nullable=False),
        sa.Column("budget_profile", sa.String(length=64), nullable=False),
        sa.Column(
            "validation_summary_json",
            postgresql.JSONB(none_as_null=True),
            nullable=False,
        ),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["product_families.family_id"],
            name=op.f("fk_product_family_versions_family_id_product_families"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["family_id", "parent_family_version_id"],
            [
                "product_family_versions.family_id",
                "product_family_versions.family_version_id",
            ],
            name="fk_product_family_versions_same_family_parent",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "version_no > 0",
            name=op.f("ck_product_family_versions_version_no_positive"),
        ),
        sa.CheckConstraint(
            "parent_family_version_id IS NULL OR parent_family_version_id <> family_version_id",
            name=op.f("ck_product_family_versions_parent_not_self"),
        ),
        sa.CheckConstraint(
            "display_name <> '' AND display_name = trim(display_name)",
            name=op.f("ck_product_family_versions_display_name_valid"),
        ),
        sa.CheckConstraint(
            "purpose <> '' AND purpose = trim(purpose)",
            name=op.f("ck_product_family_versions_purpose_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(family_contract_json) = 'object' "
            "AND family_contract_json ->> 'schema' = 'product_family_contract_v1' "
            "AND family_contract_json ->> 'product_structure_kind' "
            "IN ('single_product', 'semantic_assembly')",
            name=op.f("ck_product_family_versions_contract_shape"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("contract_digest"),
            name=op.f("ck_product_family_versions_contract_digest_shape"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("content_digest"),
            name=op.f("ck_product_family_versions_content_digest_shape"),
        ),
        sa.CheckConstraint(
            "program_entry = 'main.py:realize'",
            name=op.f("ck_product_family_versions_program_entry_valid"),
        ),
        sa.CheckConstraint(
            _lower_slug_check("runtime_profile"),
            name=op.f("ck_product_family_versions_runtime_profile_valid"),
        ),
        sa.CheckConstraint(
            _lower_slug_check("budget_profile"),
            name=op.f("ck_product_family_versions_budget_profile_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(validation_summary_json) = 'object'",
            name=op.f("ck_product_family_versions_validation_summary_object"),
        ),
        sa.PrimaryKeyConstraint(
            "family_version_id",
            name=op.f("pk_product_family_versions"),
        ),
        sa.UniqueConstraint(
            "family_id",
            "version_no",
            name="uq_product_family_versions_family_version_no",
        ),
        sa.UniqueConstraint(
            "family_id",
            "family_version_id",
            name="uq_product_family_versions_family_exact_version",
        ),
    )
    op.create_index(
        "ix_product_family_versions_family_created",
        "product_family_versions",
        ("family_id", "created_at"),
    )

    op.create_table(
        "product_family_geometry_bindings",
        sa.Column("family_version_id", sa.String(length=80), nullable=False),
        sa.Column("binding_key", sa.String(length=64), nullable=False),
        sa.Column("geometry_resource_id", sa.String(length=80), nullable=False),
        sa.Column("geometry_resource_version_id", sa.String(length=80), nullable=False),
        sa.Column("geometry_content_digest", sa.String(length=64), nullable=False),
        sa.Column("expected_resource_kind", sa.String(length=32), nullable=False),
        sa.Column("geometry_purpose", sa.String(length=32), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["family_version_id"],
            ["product_family_versions.family_version_id"],
            name="fk_product_family_bindings_family_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["geometry_resource_id", "geometry_resource_version_id"],
            [
                "geometry_resource_versions.resource_id",
                "geometry_resource_versions.resource_version_id",
            ],
            name="fk_product_family_bindings_exact_geometry_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _lower_slug_check("binding_key"),
            name=op.f("ck_product_family_geometry_bindings_binding_key_valid"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("geometry_content_digest"),
            name=op.f("ck_product_family_geometry_bindings_geometry_digest_shape"),
        ),
        sa.CheckConstraint(
            "expected_resource_kind IN ('mapped_model', 'modeling_function')",
            name=op.f("ck_product_family_geometry_bindings_resource_kind_valid"),
        ),
        sa.CheckConstraint(
            "geometry_purpose IN ('product_component', 'shape_fragment', 'interface_adapter')",
            name=op.f("ck_product_family_geometry_bindings_geometry_purpose_valid"),
        ),
        sa.PrimaryKeyConstraint(
            "family_version_id",
            "binding_key",
            name=op.f("pk_product_family_geometry_bindings"),
        ),
    )
    op.create_index(
        "ix_product_family_bindings_geometry_version",
        "product_family_geometry_bindings",
        ("geometry_resource_id", "geometry_resource_version_id"),
    )

    op.create_table(
        "product_family_version_assets",
        sa.Column("family_version_id", sa.String(length=80), nullable=False),
        sa.Column("asset_role", sa.String(length=64), nullable=False),
        sa.Column("asset_format", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["family_version_id"],
            ["product_family_versions.family_version_id"],
            name="fk_product_family_assets_family_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "asset_role IN ('realization_source_bundle', 'validation_report')",
            name=op.f("ck_product_family_version_assets_asset_role_valid"),
        ),
        sa.CheckConstraint(
            "asset_format <> '' AND asset_format = trim(asset_format)",
            name=op.f("ck_product_family_version_assets_asset_format_valid"),
        ),
        sa.CheckConstraint(
            "filename <> '' AND filename = trim(filename) "
            "AND strpos(filename, '/') = 0 AND strpos(filename, chr(92)) = 0",
            name=op.f("ck_product_family_version_assets_filename_valid"),
        ),
        sa.CheckConstraint(
            "content_type <> '' AND content_type = trim(content_type)",
            name=op.f("ck_product_family_version_assets_content_type_valid"),
        ),
        sa.CheckConstraint(
            "size_bytes > 0",
            name=op.f("ck_product_family_version_assets_size_bytes_positive"),
        ),
        sa.CheckConstraint(
            _lower_hex_check("sha256"),
            name=op.f("ck_product_family_version_assets_sha256_shape"),
        ),
        sa.CheckConstraint(
            "asset_role <> 'realization_source_bundle' OR "
            "(asset_format = 'source_bundle_v1' AND content_type = 'application/zip')",
            name=op.f("ck_product_family_version_assets_source_bundle_shape"),
        ),
        sa.PrimaryKeyConstraint(
            "family_version_id",
            "asset_role",
            name=op.f("pk_product_family_version_assets"),
        ),
    )
    op.create_index(
        "ix_product_family_version_assets_sha256",
        "product_family_version_assets",
        ("sha256",),
    )

    _create_binding_guard()
    _create_completeness_guard()
    _enable_rls_and_grant_reads()


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            LOCK TABLE
                product_families,
                product_family_versions,
                product_family_geometry_bindings,
                product_family_version_assets
            IN ACCESS EXCLUSIVE MODE
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                families_count bigint;
                versions_count bigint;
                bindings_count bigint;
                assets_count bigint;
            BEGIN
                SELECT count(*) INTO families_count FROM product_families;
                SELECT count(*) INTO versions_count FROM product_family_versions;
                SELECT count(*) INTO bindings_count
                FROM product_family_geometry_bindings;
                SELECT count(*) INTO assets_count FROM product_family_version_assets;
                IF families_count <> 0 OR versions_count <> 0
                   OR bindings_count <> 0 OR assets_count <> 0 THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = format(
                            'ProductFamily catalog is not empty: product_families=%s, '
                            'product_family_versions=%s, '
                            'product_family_geometry_bindings=%s, '
                            'product_family_version_assets=%s',
                            families_count,
                            versions_count,
                            bindings_count,
                            assets_count
                        );
                END IF;
            END
            $$
            """
        )
    )
    tables = ", ".join(_TABLES)
    for role in _RUNTIME_READ_ROLES:
        op.execute(sa.text(f"REVOKE SELECT ON TABLE {tables} FROM {role}"))
    op.execute(sa.text(f"REVOKE SELECT ON TABLE {tables} FROM {_SYSTEM_CURATOR_ROLE}"))
    op.drop_table("product_family_version_assets")
    op.drop_table("product_family_geometry_bindings")
    op.drop_table("product_family_versions")
    op.drop_table("product_families")
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_product_family_version_complete()"))
    op.execute(sa.text("DROP FUNCTION cad2bim_guard_product_family_geometry_binding()"))
