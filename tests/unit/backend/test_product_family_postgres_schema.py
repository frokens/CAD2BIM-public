from __future__ import annotations

import io

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from CAD2BIM_backend.infrastructure.postgres.models import Base


TABLES = (
    "product_families",
    "product_family_versions",
    "product_family_geometry_bindings",
    "product_family_version_assets",
)


def _render(direction: str) -> str:
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = io.StringIO()
    config.output_buffer = output
    if direction == "upgrade":
        command.upgrade(config, "20260904_0072:20260904_0073", sql=True)
    else:
        command.downgrade(config, "20260904_0073:20260904_0072", sql=True)
    return " ".join(output.getvalue().split())


def _checks(table_name: str) -> str:
    return " ".join(
        str(constraint.sqltext)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, CheckConstraint)
    )


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


def _unique_columns(table_name: str) -> set[tuple[str, ...]]:
    return {
        tuple(constraint.columns.keys())
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_product_family_metadata_has_four_exact_relations_and_no_current() -> None:
    assert all(name in Base.metadata.tables for name in TABLES)
    assert set(Base.metadata.tables["product_families"].c.keys()) == {
        "family_id",
        "owner_scope",
        "owner_tenant_id",
        "owner_task_id",
        "draft_generation",
        "draft_artifact_version_id",
        "deleted_at",
        "created_at",
    }
    assert set(Base.metadata.tables["product_family_versions"].c.keys()) == {
        "family_version_id",
        "family_id",
        "version_no",
        "parent_family_version_id",
        "display_name",
        "purpose",
        "usage_instructions",
        "family_contract_json",
        "contract_digest",
        "content_digest",
        "program_entry",
        "created_at",
    }
    assert set(Base.metadata.tables["product_family_geometry_bindings"].c.keys()) == {
        "family_version_id",
        "binding_key",
        "geometry_resource_id",
        "geometry_resource_version_id",
        "geometry_content_digest",
        "expected_resource_kind",
        "geometry_purpose",
        "created_at",
    }
    assert set(Base.metadata.tables["product_family_version_assets"].c.keys()) == {
        "family_version_id",
        "asset_role",
        "asset_format",
        "filename",
        "content_type",
        "size_bytes",
        "sha256",
        "created_at",
    }
    product_columns = {
        column.name for table_name in TABLES for column in Base.metadata.tables[table_name].columns
    }
    assert not any(name.startswith("current") for name in product_columns)
    assert "default_version_id" not in product_columns
    assert "latest_version_id" not in product_columns
    assert "realization_programs" not in Base.metadata.tables
    assert "realization_program_versions" not in Base.metadata.tables


def test_product_family_metadata_binds_same_root_parent_and_exact_geometry_pair() -> None:
    assert (
        ("family_id",),
        ("product_families.family_id",),
        "RESTRICT",
    ) in _foreign_keys("product_family_versions")
    assert (
        ("family_id", "parent_family_version_id"),
        (
            "product_family_versions.family_id",
            "product_family_versions.family_version_id",
        ),
        "RESTRICT",
    ) in _foreign_keys("product_family_versions")
    assert (
        ("geometry_resource_id", "geometry_resource_version_id"),
        (
            "geometry_resource_versions.resource_id",
            "geometry_resource_versions.resource_version_id",
        ),
        "RESTRICT",
    ) in _foreign_keys("product_family_geometry_bindings")
    assert (
        ("family_version_id",),
        ("product_family_versions.family_version_id",),
        "RESTRICT",
    ) in _foreign_keys("product_family_geometry_bindings")
    assert (
        ("family_version_id",),
        ("product_family_versions.family_version_id",),
        "RESTRICT",
    ) in _foreign_keys("product_family_version_assets")

    assert _unique_columns("product_family_versions") == {
        ("family_id", "version_no"),
        ("family_id", "family_version_id"),
    }
    assert tuple(
        Base.metadata.tables["product_family_geometry_bindings"].primary_key.columns.keys()
    ) == (
        "family_version_id",
        "binding_key",
    )
    assert tuple(
        Base.metadata.tables["product_family_version_assets"].primary_key.columns.keys()
    ) == (
        "family_version_id",
        "asset_role",
    )


def test_product_family_metadata_closes_scope_content_and_asset_shapes() -> None:
    roots = _checks("product_families")
    assert "owner_scope IN ('SYSTEM', 'TENANT', 'TASK')" in roots
    assert "owner_scope = 'SYSTEM' AND owner_tenant_id IS NULL" in roots
    assert "owner_scope = 'TENANT' AND owner_tenant_id IS NOT NULL" in roots
    assert "owner_scope = 'TASK' AND owner_tenant_id IS NOT NULL" in roots

    versions = _checks("product_family_versions")
    assert "version_no > 0" in versions
    assert "parent_family_version_id <> family_version_id" in versions
    assert "program_entry = 'main.py:realize'" in versions
    assert "json_type(family_contract_json) = 'object'" in versions
    assert "product_family_contract_v" not in versions
    assert "contract_digest = lower(contract_digest)" in versions
    assert "content_digest = lower(content_digest)" in versions

    bindings = _checks("product_family_geometry_bindings")
    assert "length(binding_key) BETWEEN 1 AND 64" in bindings
    assert "substr(binding_key, 1, 1) BETWEEN 'a' AND 'z'" in bindings
    assert "expected_resource_kind IN ('mapped_model', 'modeling_function')" in bindings
    assert (
        "geometry_purpose IN ('product_component', 'shape_fragment', 'interface_adapter')"
        in bindings
    )
    assert "geometry_content_digest = lower(geometry_content_digest)" in bindings

    assets = _checks("product_family_version_assets")
    assert "asset_role IN ('realization_source_bundle', 'validation_report', 'standard_model_geometry', 'standard_model_manifest')" in assets
    assert "size_bytes > 0" in assets
    assert "sha256 = lower(sha256)" in assets
    assert "asset_format = 'source_bundle_v1'" in assets
    assert "content_type = 'application/zip'" in assets


def test_0073_upgrade_creates_rls_catalog_scope_guard_and_minimum_grants() -> None:
    sql = _render("upgrade")
    for table_name in TABLES:
        assert f"CREATE TABLE {table_name}" in sql
        assert f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in sql
    assert "current_setting('cad2bim.tenant_id', true)" in sql
    assert "current_setting('cad2bim.task_id', true)" in sql
    assert "owner_scope = 'SYSTEM'" in sql
    assert "owner_scope = 'TENANT'" in sql
    assert "owner_scope = 'TASK'" in sql
    assert "geometry_resource_versions" in sql
    assert "product_family_geometry_bindings" in sql
    assert "realization_source_bundle" in sql
    assert "FOR SELECT TO cad2bim_api_runtime, cad2bim_runner_runtime, cad2bim_mcp_runtime" in sql
    assert "GRANT SELECT" in sql
    assert "GRANT INSERT" not in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
    assert "FOR INSERT TO" not in sql
    assert "runtime_insert" not in sql
    assert "system_insert" not in sql
    assert "CREATE TABLE realization_program" not in sql
    assert "CREATE TABLE run_component_views" not in sql


def test_0073_downgrade_locks_and_fails_closed_without_restoring_legacy_product_core() -> None:
    sql = _render("downgrade")
    assert "IN ACCESS EXCLUSIVE MODE" in sql
    for table_name in TABLES:
        assert f"{table_name}=%s" in sql
    assert "ERRCODE = '23514'" in sql
    assert "DELETE FROM" not in sql.upper()
    assert "CASCADE" not in sql.upper()
    assert "CREATE TABLE run_component_views" not in sql
    assert "CREATE TABLE component_catalog_events" not in sql
    drops = [sql.index(f"DROP TABLE {table_name}") for table_name in TABLES]
    assert drops == sorted(drops, reverse=True)




def test_binding_option_migration_admits_new_rows_and_retains_older_immutable_rows():
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = io.StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260924_0089:20260927_0090", sql=True)
    sql = output.getvalue()
    assert "product_family_contract_v3" in sql
    assert "product_family_contract_v4" in sql
    assert "product_realization_v3" in sql
    assert "product_realization_v4" in sql
    assert "cad2bim_guard_product_family_current_version" in sql
    assert "cad2bim_guard_product_family_current_operation" in sql
    assert "cad2bim_guard_product_family_authoring_operation" in sql
