from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from CAD2BIM_backend.infrastructure.postgres.models import Base


LEGACY_PRODUCT_TABLES = (
    "run_component_views",
    "component_catalog_events",
    "component_publication_receipts",
    "component_promotions",
)
GEOMETRY_TABLES = (
    "geometry_resources",
    "geometry_resource_versions",
    "geometry_resource_version_assets",
)


def _render_upgrade(start: str, end: str) -> str:
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, f"{start}:{end}", sql=True)
    return " ".join(output.getvalue().split())


def _render_downgrade(start: str, end: str) -> str:
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    command.downgrade(config, f"{start}:{end}", sql=True)
    return " ".join(output.getvalue().split())


def _check_sql(table_name: str) -> str:
    return " ".join(
        str(constraint.sqltext)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, CheckConstraint)
    )


def _unique_columns(table_name: str) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _foreign_keys(
    table_name: str,
) -> set[tuple[tuple[str, ...], tuple[str, ...], str | None]]:
    return {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
            constraint.ondelete,
        )
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }


def test_geometry_resource_metadata_replaces_only_the_legacy_product_core() -> None:
    assert all(name in Base.metadata.tables for name in GEOMETRY_TABLES)
    assert all(name not in Base.metadata.tables for name in LEGACY_PRODUCT_TABLES)
    assert {
        "product_families",
        "product_family_versions",
        "product_family_geometry_bindings",
        "product_family_version_assets",
    } <= set(Base.metadata.tables)

    resources = Base.metadata.tables["geometry_resources"]
    assert set(resources.c.keys()) == {
        "resource_id",
        "resource_kind",
        "owner_scope",
        "owner_tenant_id",
        "owner_task_id",
        "draft_generation",
        "draft_artifact_version_id",
        "display_name",
        "created_at",
        "deleted_at",
    }
    versions = Base.metadata.tables["geometry_resource_versions"]
    assert set(versions.c.keys()) == {
        "resource_version_id",
        "resource_id",
        "version_no",
        "origin_kind",
        "content_digest",
        "manifest_digest",
        "manifest_json",
        "lineage_json",
        "created_at",
    }
    assets = Base.metadata.tables["geometry_resource_version_assets"]
    assert set(assets.c.keys()) == {
        "resource_version_id",
        "asset_role",
        "asset_format",
        "filename",
        "content_type",
        "size_bytes",
        "sha256",
        "created_at",
    }
    assert "storage_key" not in assets.c


def test_geometry_resource_metadata_has_exact_scope_lineage_and_immutability_shape() -> None:
    root_checks = _check_sql("geometry_resources")
    assert "resource_kind IN ('mapped_model', 'modeling_function')" in root_checks
    assert "owner_scope IN ('SYSTEM', 'TENANT', 'TASK')" in root_checks
    assert "owner_scope = 'SYSTEM' AND owner_tenant_id IS NULL" in root_checks
    assert "owner_scope = 'TASK' AND owner_tenant_id IS NOT NULL" in root_checks

    assert (
        ("owner_tenant_id", "owner_task_id"),
        ("tasks.tenant_id", "tasks.task_id"),
        "RESTRICT",
    ) in _foreign_keys("geometry_resources")
    assert (
        ("resource_id",),
        ("geometry_resources.resource_id",),
        "RESTRICT",
    ) in _foreign_keys("geometry_resource_versions")
    assert (
        ("resource_version_id",),
        ("geometry_resource_versions.resource_version_id",),
        "RESTRICT",
    ) in _foreign_keys("geometry_resource_version_assets")

    assert _unique_columns("geometry_resources") == set()
    assert _unique_columns("geometry_resource_versions") == {
        ("resource_id", "version_no"),
        ("resource_id", "resource_version_id"),
    }
    version_checks = _check_sql("geometry_resource_versions")
    assert "version_no > 0" in version_checks
    assert "content_digest = lower(content_digest)" in version_checks
    assert "manifest_digest = lower(manifest_digest)" in version_checks
    asset_checks = _check_sql("geometry_resource_version_assets")
    assert "asset_role IN ('canonical_geometry', 'manifest', 'validation_report'" in asset_checks
    assert "size_bytes > 0" in asset_checks
    assert "sha256 = lower(sha256)" in asset_checks

    root_indexes = Base.metadata.tables["geometry_resources"].indexes
    assert any(index.name == "ix_geometry_resources_scope_kind_created" for index in root_indexes)
    version_indexes = Base.metadata.tables["geometry_resource_versions"].indexes
    assert any(
        index.name == "ix_geometry_resource_versions_resource_created" for index in version_indexes
    )


def test_0065_upgrade_locks_counts_and_only_drops_empty_legacy_tables() -> None:
    sql = _render_upgrade("20260830_0064", "20260902_0065")

    assert "IN ACCESS EXCLUSIVE MODE" in sql
    for table in LEGACY_PRODUCT_TABLES:
        assert f"SELECT count(*) INTO {table}_count FROM {table}" in sql
        assert f"{table}=%s" in sql
    assert "ERRCODE = '23514'" in sql
    assert "DELETE FROM" not in sql.upper()
    assert "IF EXISTS" not in sql.upper()
    assert "CASCADE" not in sql.upper()
    drops = [
        sql.index(f"DROP TABLE {table}")
        for table in (
            "component_publication_receipts",
            "component_promotions",
            "component_catalog_events",
            "run_component_views",
        )
    ]
    assert drops == sorted(drops)


def test_0065_downgrade_is_explicitly_irreversible() -> None:
    sql = _render_downgrade("20260902_0065", "20260830_0064")
    assert "ERRCODE = '0A000'" in sql
    assert "intentionally irreversible" in sql
    assert "CREATE TABLE run_component_views" not in sql


def test_0066_upgrade_creates_scoped_rls_catalog_with_minimum_grants() -> None:
    sql = _render_upgrade("20260902_0065", "20260902_0066")

    for table in GEOMETRY_TABLES:
        assert f"CREATE TABLE {table}" in sql
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
    assert "current_setting('cad2bim.tenant_id', true)" in sql
    assert "current_setting('cad2bim.task_id', true)" in sql
    assert "owner_scope = 'SYSTEM'" in sql
    assert "owner_scope = 'TENANT'" in sql
    assert "owner_scope = 'TASK'" in sql
    assert "EXISTS (SELECT 1 FROM geometry_resources AS resource" in sql
    assert "JOIN geometry_resources AS resource" in sql
    assert "FOR SELECT TO cad2bim_api_runtime, cad2bim_runner_runtime, cad2bim_mcp_runtime" in sql
    assert "FOR INSERT TO cad2bim_runner_runtime, cad2bim_mcp_runtime" in sql
    assert "FOR INSERT TO cad2bim_system_curator" in sql
    assert "geometry_resource_versions.resource_id AND resource.owner_scope = 'TASK'" in sql
    assert (
        "geometry_resource_version_assets.resource_version_id "
        "AND resource.owner_scope = 'TASK'" in sql
    )
    assert "resource.owner_scope = 'SYSTEM'" in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
    assert "CREATE TABLE product_famil" not in sql


def test_0066_downgrade_only_drops_an_empty_catalog() -> None:
    sql = _render_downgrade("20260902_0066", "20260902_0065")
    assert "IN ACCESS EXCLUSIVE MODE" in sql
    assert "ERRCODE = '23514'" in sql
    assert "geometry_resources=%s" in sql
    assert "geometry_resource_versions=%s" in sql
    assert "geometry_resource_version_assets=%s" in sql
    assert "DELETE FROM" not in sql.upper()
    drops = [
        sql.index(f"DROP TABLE {table}")
        for table in (
            "geometry_resource_version_assets",
            "geometry_resource_versions",
            "geometry_resources",
        )
    ]
    assert drops == sorted(drops)
