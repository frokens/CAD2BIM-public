from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError


pytestmark = [pytest.mark.integration, pytest.mark.network]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL")
RUNTIME_ROLES = {
    "cad2bim_api_runtime",
    "cad2bim_runner_runtime",
    "cad2bim_system_curator",
}
CURRENT_TENANT_POLICY_TABLES = {
    "artifact_versions",
    "artifacts",
    "checkpoint_links",
    "checkpoint_projection_receipts",
    "component_catalog_events",
    "component_publication_receipts",
    "component_promotions",
    "feedback_commands",
    "feedback_delivery_batch_members",
    "feedback_delivery_batches",
    "ifc_run_projections",
    "ifc_run_stage_projections",
    "interrupts",
    "mcp_sessions",
    "mcp_snapshots",
    "run_artifact_views",
    "run_component_views",
    "run_events",
    "run_resume_operations",
    "run_stage_business_views",
    "runs",
    "safe_point_publications",
    "stage_plans",
    "task_tenant_bindings",
    "tasks",
    "tenant_principals",
    "tenants",
    "users",
    "user_savepoint_commands",
}
RLS_PROTECTED_TABLES = CURRENT_TENANT_POLICY_TABLES | {
    "feedback_migration_quarantine"
}
TENANT_OWNED_TABLES = CURRENT_TENANT_POLICY_TABLES - {
    "task_tenant_bindings",
    "tenant_principals",
    "tenants",
}


def _sync_database_url(value: str) -> str:
    return (
        make_url(value)
        .set(drivername="postgresql+psycopg")
        .render_as_string(hide_password=False)
    )


def _set_runtime_tenant(connection, tenant_id: str) -> None:
    connection.execute(text("SET ROLE cad2bim_api_runtime"))
    connection.execute(
        text("SELECT set_config('cad2bim.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_id},
    )


def _legacy_identity_id(prefix: str, namespace: str, owner_id: str) -> str:
    digest = hashlib.sha256(f"{namespace}:{owner_id}".encode()).hexdigest()
    return f"{prefix}{digest}"


def test_identity_tenant_migration_backfills_and_forces_non_superuser_rls() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    schema = f"identity_tenant_rls_{uuid4().hex}"
    admin_engine = create_engine(_sync_database_url(TEST_DATABASE_URL), pool_pre_ping=True)
    migration_url = make_url(_sync_database_url(TEST_DATABASE_URL)).update_query_dict(
        {"options": f"-csearch_path={schema}"}
    )
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        migration_url.render_as_string(hide_password=False).replace("%", "%%"),
    )

    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        command.upgrade(config, "20260713_0015")
        scoped_engine = create_engine(
            migration_url,
            pool_pre_ping=True,
            pool_size=1,
            max_overflow=0,
        )
        try:
            with scoped_engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users (owner_id) VALUES ('legacy-a'), ('legacy-b'); "
                        "INSERT INTO tasks ("
                        "task_id, owner_id, title, metadata_json, status, active_module"
                        ") VALUES "
                        "('task-a', 'legacy-a', 'A', '{}'::jsonb, 'draft', 'workflow'), "
                        "('task-b', 'legacy-b', 'B', '{}'::jsonb, 'draft', 'workflow'); "
                        "INSERT INTO runs ("
                        "run_id, owner_id, task_id, module, stage_key, input_payload, "
                        "idempotency_key, request_fingerprint, thread_id, status"
                        ") VALUES "
                        "('run-a', 'legacy-a', 'task-a', 'workflow', 'cad2bim_workflow', "
                        "'{}'::jsonb, 'run-a-key', repeat('a', 64), 'thread-a', 'completed'), "
                        "('run-b', 'legacy-b', 'task-b', 'workflow', 'cad2bim_workflow', "
                        "'{}'::jsonb, 'run-b-key', repeat('b', 64), 'thread-b', 'completed')"
                    )
                )

            command.upgrade(config, "20260719_0020")
            with scoped_engine.begin() as connection:
                connection.execute(
                    text("UPDATE runs SET tenant_id = NULL WHERE run_id = 'run-a'")
                )
            command.upgrade(config, "head")

            with scoped_engine.connect() as connection:
                mappings = connection.execute(
                    text(
                        "SELECT p.legacy_owner_id, p.principal_id, p.status, "
                        "t.tenant_id, tp.access "
                        "FROM principals p "
                        "JOIN tenant_principals tp USING (principal_id) "
                        "JOIN tenants t USING (tenant_id) "
                        "ORDER BY p.legacy_owner_id"
                    )
                ).all()
                assert len(mappings) == 2
                assert [(row[0], row[2], row[4]) for row in mappings] == [
                    ("legacy-a", "disabled", "owner"),
                    ("legacy-b", "disabled", "owner"),
                ]
                tenant_by_owner = {row[0]: row[3] for row in mappings}
                principal_by_owner = {row[0]: row[1] for row in mappings}
                assert tenant_by_owner["legacy-a"] == _legacy_identity_id(
                    "tenant_", "legacy-tenant", "legacy-a"
                )
                assert principal_by_owner["legacy-a"] == _legacy_identity_id(
                    "principal_", "legacy-principal", "legacy-a"
                )
                assert tenant_by_owner["legacy-a"] != tenant_by_owner["legacy-b"]
                assert connection.execute(
                    text("SELECT owner_id, tenant_id FROM tasks ORDER BY owner_id")
                ).all() == [
                    ("legacy-a", tenant_by_owner["legacy-a"]),
                    ("legacy-b", tenant_by_owner["legacy-b"]),
                ]
                assert connection.execute(
                    text("SELECT owner_id, tenant_id FROM runs ORDER BY owner_id")
                ).all() == [
                    ("legacy-a", tenant_by_owner["legacy-a"]),
                    ("legacy-b", tenant_by_owner["legacy-b"]),
                ]
                nullability = dict(
                    connection.execute(
                        text(
                            "SELECT table_name, is_nullable "
                            "FROM information_schema.columns "
                            "WHERE table_schema = current_schema() "
                            "AND column_name = 'tenant_id' "
                            "AND table_name = ANY(:tables)"
                        ),
                        {"tables": sorted(TENANT_OWNED_TABLES)},
                    ).all()
                )
                assert nullability == {
                    table_name: "NO" for table_name in TENANT_OWNED_TABLES
                }
                constraint_names = set(
                    connection.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE connamespace = current_schema()::regnamespace"
                        )
                    ).scalars()
                )
                assert {
                    "uq_users_owner_tenant",
                    "uq_tasks_tenant_task",
                    "fk_tasks_owner_tenant_users",
                    "fk_task_tenant_bindings_tenant_task_tasks",
                    "fk_runs_tenant_task_tasks",
                    "fk_run_events_tenant_task_tasks",
                    "fk_artifact_versions_tenant_task_tasks",
                    "fk_mcp_snapshots_tenant_task_tasks",
                    "fk_checkpoint_links_tenant_task_tasks",
                } <= constraint_names
                assert connection.execute(
                    text(
                        "SELECT legacy_owner_id, reason_code, task_count "
                        "FROM identity_migration_quarantine ORDER BY legacy_owner_id"
                    )
                ).all() == [
                    ("legacy-a", "legacy.identity_issuer_unprovable", 1),
                    ("legacy-b", "legacy.identity_issuer_unprovable", 1),
                ]

                role_rows = connection.execute(
                    text(
                        "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin "
                        "FROM pg_roles WHERE rolname = ANY(:roles) ORDER BY rolname"
                    ),
                    {"roles": sorted(RUNTIME_ROLES)},
                ).all()
                assert {row[0] for row in role_rows} == RUNTIME_ROLES
                assert all(row[1:] == (False, False, False) for row in role_rows)
                assert connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'cad2bim_system_curator', current_schema() || '.tasks', 'SELECT'"
                        ")"
                    )
                ).scalar_one() is False
                assert connection.execute(
                    text(
                        "SELECT has_function_privilege("
                        "'cad2bim_api_runtime', "
                        "current_schema() || '.cad2bim_resolve_identity(text,text,text,text)', "
                        "'EXECUTE'"
                        ")"
                    )
                ).scalar_one() is True
                assert connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'cad2bim_api_runtime', "
                        "current_schema() || '.component_catalog_events', "
                        "'SELECT,INSERT'"
                        ")"
                    )
                ).scalar_one() is True
                assert connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'cad2bim_api_runtime', "
                        "current_schema() || '.component_catalog_events', "
                        "'UPDATE,DELETE'"
                        ")"
                    )
                ).scalar_one() is False
                assert connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'cad2bim_api_runtime', "
                        "current_schema() || '.component_publication_receipts', "
                        "'SELECT,INSERT')"
                    )
                ).scalar_one() is True
                assert connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'cad2bim_api_runtime', "
                        "current_schema() || '.component_publication_receipts', "
                        "'UPDATE,DELETE')"
                    )
                ).scalar_one() is False
                assert connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'cad2bim_runner_runtime', "
                        "current_schema() || '.component_publication_receipts', "
                        "'SELECT')"
                    )
                ).scalar_one() is True
                assert connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'cad2bim_runner_runtime', "
                        "current_schema() || '.component_publication_receipts', "
                        "'INSERT,UPDATE,DELETE')"
                    )
                ).scalar_one() is False
                assert connection.execute(
                    text(
                        "SELECT has_function_privilege("
                        "'cad2bim_runner_runtime', "
                        "current_schema() || '.cad2bim_claim_next_run(text,integer)', "
                        "'EXECUTE'"
                        ")"
                    )
                ).scalar_one() is True

                secured_tables = connection.execute(
                    text(
                        "SELECT c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = current_schema() "
                        "AND c.relrowsecurity AND c.relforcerowsecurity "
                        "ORDER BY c.relname"
                    )
                ).scalars().all()
                assert set(secured_tables) == RLS_PROTECTED_TABLES
                policy_tables = connection.execute(
                    text(
                        "SELECT tablename FROM pg_policies "
                        "WHERE schemaname = current_schema() AND policyname = 'tenant_isolation'"
                    )
                ).scalars().all()
                assert set(policy_tables) == RLS_PROTECTED_TABLES

            with scoped_engine.begin() as connection:
                connection.execute(text("SET ROLE cad2bim_api_runtime"))
                assert connection.execute(text("SELECT task_id FROM tasks")).scalars().all() == []
                connection.execute(text("RESET ROLE"))

            with scoped_engine.begin() as connection:
                _set_runtime_tenant(connection, tenant_by_owner["legacy-a"])
                assert connection.execute(text("SELECT task_id FROM tasks")).scalars().all() == [
                    "task-a"
                ]
                connection.execute(text("RESET ROLE"))

            with scoped_engine.begin() as connection:
                connection.execute(text("SET ROLE cad2bim_api_runtime"))
                assert connection.execute(text("SELECT task_id FROM tasks")).scalars().all() == []
                connection.execute(
                    text("SELECT set_config('cad2bim.tenant_id', :tenant_id, true)"),
                    {"tenant_id": tenant_by_owner["legacy-b"]},
                )
                assert connection.execute(text("SELECT task_id FROM tasks")).scalars().all() == [
                    "task-b"
                ]
                connection.execute(text("RESET ROLE"))

            with pytest.raises(DBAPIError):
                with scoped_engine.begin() as connection:
                    _set_runtime_tenant(connection, tenant_by_owner["legacy-a"])
                    connection.execute(
                        text(
                            "INSERT INTO tasks ("
                            "task_id, owner_id, tenant_id, title, metadata_json, status, "
                            "active_module"
                            ") VALUES ("
                            "'cross-tenant', 'legacy-b', :tenant_id, 'forged', '{}'::jsonb, "
                            "'draft', 'workflow')"
                        ),
                        {"tenant_id": tenant_by_owner["legacy-b"]},
                    )

            command.downgrade(config, "20260719_0020")
            with scoped_engine.connect() as connection:
                assert connection.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'runs' AND column_name = 'tenant_id'"
                    )
                ).scalar_one() == "YES"
                assert connection.execute(
                    text(
                        "SELECT count(*) FROM pg_constraint "
                        "WHERE connamespace = current_schema()::regnamespace "
                        "AND conname = 'fk_runs_tenant_task_tasks'"
                    )
                ).scalar_one() == 0
            command.upgrade(config, "head")
        finally:
            scoped_engine.dispose()
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def test_tenant_scope_hardening_fails_closed_on_owner_tenant_mismatch() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    schema = f"tenant_scope_mismatch_{uuid4().hex}"
    admin_engine = create_engine(_sync_database_url(TEST_DATABASE_URL), pool_pre_ping=True)
    migration_url = make_url(_sync_database_url(TEST_DATABASE_URL)).update_query_dict(
        {"options": f"-csearch_path={schema}"}
    )
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        migration_url.render_as_string(hide_password=False).replace("%", "%%"),
    )

    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        command.upgrade(config, "20260713_0015")
        scoped_engine = create_engine(migration_url, pool_pre_ping=True)
        try:
            with scoped_engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users (owner_id) VALUES ('legacy-a'), ('legacy-b'); "
                        "INSERT INTO tasks ("
                        "task_id, owner_id, title, metadata_json, status, active_module"
                        ") VALUES "
                        "('task-a', 'legacy-a', 'A', '{}'::jsonb, 'draft', 'workflow'), "
                        "('task-b', 'legacy-b', 'B', '{}'::jsonb, 'draft', 'workflow')"
                    )
                )
            command.upgrade(config, "20260719_0020")
            with scoped_engine.begin() as connection:
                tenant_b = connection.execute(
                    text("SELECT tenant_id FROM tenants WHERE legacy_owner_id = 'legacy-b'")
                ).scalar_one()
                connection.execute(
                    text("UPDATE tasks SET tenant_id = :tenant_id WHERE task_id = 'task-a'"),
                    {"tenant_id": tenant_b},
                )

            with pytest.raises(DBAPIError, match="tasks.owner_tenant_mismatch"):
                command.upgrade(config, "head")

            with scoped_engine.connect() as connection:
                assert connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one() == "20260719_0020"
        finally:
            scoped_engine.dispose()
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()
