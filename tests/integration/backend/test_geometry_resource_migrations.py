from __future__ import annotations

import os
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError

from CAD2BIM_backend.infrastructure.postgres.database import EXPECTED_SCHEMA_REVISION


pytestmark = [pytest.mark.integration, pytest.mark.network]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL")

_LEGACY_TABLES = (
    "run_component_views",
    "component_catalog_events",
    "component_publication_receipts",
    "component_promotions",
)
_GEOMETRY_TABLES = (
    "geometry_resources",
    "geometry_resource_versions",
    "geometry_resource_version_assets",
)
_IFC_QUERY_TABLES = ("ifc_query_attempts", "ifc_query_inputs")
_AUTHORING_COLUMNS = {
    "target_geometry_resource_id",
    "result_geometry_resource_version_id",
}
_AUTHORING_CONSTRAINTS = {
    "uq_geometry_resource_versions_resource_exact_version",
    "fk_critical_operations_exact_geometry_result_version",
}
_AUTHORING_CHECK_CONSTRAINTS = {
    "ck_critical_operations_operation_kind_valid",
    "ck_critical_operations_operation_kind_detail_valid",
    "ck_critical_operations_terminal_result_valid",
    "ck_critical_operations_error_phase_valid",
}
_AUTHORING_FUNCTIONS = {
    "cad2bim_guard_geometry_authoring_operation",
    "cad2bim_geometry_source_artifact_visible",
    "cad2bim_geometry_operation_catalog_visible",
}
_AUTHORING_POLICIES = {
    "geometry_authoring_task_isolation",
    "geometry_authoring_source_task_isolation",
    "geometry_authoring_source_version_task_isolation",
    "geometry_authoring_catalog_task_isolation",
}
_AUTHORING_WRITE_ROLES = {
    "cad2bim_runner_runtime",
    "cad2bim_mcp_runtime",
}
_AUTHORING_RLS_ROLES = _AUTHORING_WRITE_ROLES | {"cad2bim_api_runtime"}
_DIGEST = "a" * 64
_TAGGED_DIGEST = f"sha256:{_DIGEST}"


def _sync_database_url(value: str) -> str:
    return (
        make_url(value).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
    )


@contextmanager
def _isolated_database(prefix: str) -> Iterator[tuple[Config, Engine]]:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    schema = f"{prefix}_{uuid4().hex}"
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
        scoped_engine = create_engine(migration_url, pool_pre_ping=True)
        try:
            yield config, scoped_engine
        finally:
            scoped_engine.dispose()
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def _table_names(engine: Engine, names: tuple[str, ...]) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = ANY(:names)"
                ),
                {"names": list(names)},
            ).scalars()
        )


def test_publication_qualification_cleanup_preserves_actual_asset_guards() -> None:
    with _isolated_database("publication_qualification") as (config, engine):
        command.upgrade(config, "20261001_0105")
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND column_name='validation_summary_json' "
                "AND table_name IN ('geometry_resource_versions','product_family_versions')"
            )).scalar_one() == 2
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == EXPECTED_SCHEMA_REVISION
            assert connection.execute(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND column_name='validation_summary_json' "
                "AND table_name IN ('geometry_resource_versions','product_family_versions')"
            )).scalar_one() == 0
            functions = {name: connection.execute(text(
                "SELECT pg_get_functiondef(CAST(:name AS regprocedure))"
            ), {"name": name + "()"}).scalar_one() for name in (
                "cad2bim_guard_product_family_current_version",
                "cad2bim_guard_geometry_formal_version",
                "cad2bim_guard_geometry_authoring_operation",
                "cad2bim_guard_product_family_version_complete",
            )}
            assert all("validation_summary_json" not in value for value in functions.values())
            assert "NEW.usage_instructions IS NULL" in functions["cad2bim_guard_product_family_current_version"]
            assert "Geometry formal assets are incomplete" in functions["cad2bim_guard_geometry_formal_version"]
            assert "base_resource_version_id" in functions["cad2bim_guard_geometry_authoring_operation"]
            assert "standard_model_geometry" in functions["cad2bim_guard_product_family_version_complete"]


def _assert_authoring_migration_state(
    engine: Engine,
    *,
    revision: str,
    enabled: bool,
) -> None:
    expected = _AUTHORING_COLUMNS if enabled else set()
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == revision
        )

        columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'critical_operations' "
                    "AND column_name = ANY(:names)"
                ),
                {"names": sorted(_AUTHORING_COLUMNS)},
            ).scalars()
        )
        assert columns == expected

        constraints = dict(
            connection.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) "
                    "FROM pg_constraint "
                    "WHERE connamespace = current_schema()::regnamespace "
                    "AND conname = ANY(:names)"
                ),
                {"names": sorted(_AUTHORING_CONSTRAINTS | _AUTHORING_CHECK_CONSTRAINTS)},
            ).all()
        )
        assert _AUTHORING_CHECK_CONSTRAINTS <= constraints.keys()
        assert (_AUTHORING_CONSTRAINTS <= constraints.keys()) is enabled
        for constraint_name in _AUTHORING_CHECK_CONSTRAINTS:
            assert ("geometry_resource_write" in constraints[constraint_name]) is enabled

        index_predicate = connection.execute(
            text(
                "SELECT pg_get_expr(i.indpred, i.indrelid) "
                "FROM pg_index AS i "
                "JOIN pg_class AS index_relation ON index_relation.oid = i.indexrelid "
                "JOIN pg_namespace AS namespace "
                "ON namespace.oid = index_relation.relnamespace "
                "WHERE namespace.nspname = current_schema() "
                "AND index_relation.relname = "
                "'uq_critical_operations_run_tool_fingerprint'"
            )
        ).scalar_one()
        assert ("geometry_resource_write" in index_predicate) is enabled

        trigger_names = set(
            connection.execute(
                text(
                    "SELECT trigger_name FROM information_schema.triggers "
                    "WHERE trigger_schema = current_schema() "
                    "AND event_object_table = 'critical_operations' "
                    "AND trigger_name = 'trg_geometry_authoring_operation_guard'"
                )
            ).scalars()
        )
        assert trigger_names == ({"trg_geometry_authoring_operation_guard"} if enabled else set())

        function_names = set(
            connection.execute(
                text(
                    "SELECT routine_name FROM information_schema.routines "
                    "WHERE routine_schema = current_schema() "
                    "AND routine_name = ANY(:names)"
                ),
                {"names": sorted(_AUTHORING_FUNCTIONS)},
            ).scalars()
        )
        assert function_names == (_AUTHORING_FUNCTIONS if enabled else set())

        policy_rows = dict(
            connection.execute(
                text(
                    "SELECT policyname, permissive FROM pg_policies "
                    "WHERE schemaname = current_schema() "
                    "AND policyname = ANY(:names)"
                ),
                {"names": sorted(_AUTHORING_POLICIES)},
            ).all()
        )
        assert set(policy_rows) == (_AUTHORING_POLICIES if enabled else set())
        assert all(value == "RESTRICTIVE" for value in policy_rows.values())

        column_grants = set(
            connection.execute(
                text(
                    "SELECT grantee FROM information_schema.column_privileges "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'critical_operations' "
                    "AND column_name = 'result_geometry_resource_version_id' "
                    "AND privilege_type = 'UPDATE' "
                    "AND grantee = ANY(:roles)"
                ),
                {"roles": sorted(_AUTHORING_WRITE_ROLES)},
            ).scalars()
        )
        assert column_grants == (_AUTHORING_WRITE_ROLES if enabled else set())

        if enabled:
            for function in (
                "cad2bim_geometry_source_artifact_visible(text,text,text)",
                "cad2bim_geometry_operation_catalog_visible(text,text,text)",
            ):
                for role in _AUTHORING_RLS_ROLES:
                    assert (
                        connection.execute(
                            text(
                                "SELECT has_function_privilege("
                                ":role, current_schema() || '.' || :function, 'EXECUTE')"
                            ),
                            {"role": role, "function": function},
                        ).scalar_one()
                        is True
                    )
                assert (
                    connection.execute(
                        text(
                            "SELECT has_function_privilege("
                            "'public', current_schema() || '.' || :function, 'EXECUTE')"
                        ),
                        {"function": function},
                    ).scalar_one()
                    is False
                )


def _legacy_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table_name: connection.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one()
            for table_name in _LEGACY_TABLES
        }


def _seed_identity_scope(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO principals (
                    principal_id, issuer, subject, status
                ) VALUES (
                    'principal-legacy', 'test-issuer', 'owner-legacy', 'active'
                );
                INSERT INTO tenants (tenant_id, kind, status)
                VALUES ('tenant-legacy', 'personal', 'active');
                INSERT INTO tenant_principals (tenant_id, principal_id, access)
                VALUES ('tenant-legacy', 'principal-legacy', 'owner');
                INSERT INTO users (owner_id, tenant_id)
                VALUES ('owner-legacy', 'tenant-legacy');
                INSERT INTO tasks (
                    task_id, owner_id, tenant_id, title, metadata_json,
                    status, active_module
                ) VALUES (
                    'task-legacy', 'owner-legacy', 'tenant-legacy',
                    'Legacy Product-Core fixture', '{}'::jsonb, 'running', 'workflow'
                );
                INSERT INTO runs (
                    run_id, owner_id, tenant_id, task_id, module, stage_key,
                    input_payload, idempotency_key, request_fingerprint,
                    thread_id, status, execution_protocol_status, execution_mode
                ) VALUES (
                    'run-legacy', 'owner-legacy', 'tenant-legacy', 'task-legacy',
                    'direct_mcp', 'direct_mcp', '{}'::jsonb, 'run-legacy-key',
                    repeat('b', 64), 'thread-legacy', 'running',
                    'not_applicable', 'direct_mcp'
                );
                INSERT INTO mcp_sessions (
                    session_id, owner_id, tenant_id, task_id, run_id,
                    server_name, session_storage_key, status,
                    idempotency_key, request_fingerprint
                ) VALUES (
                    'session-legacy', 'owner-legacy', 'tenant-legacy',
                    'task-legacy', 'run-legacy', 'legacy', 'legacy/session', 'active',
                    'session-legacy-key', repeat('c', 64)
                )
                """
            )
        )


def _seed_artifacts(engine: Engine) -> None:
    with engine.begin() as connection:
        for role in ("published", "preview", "validation", "receipt"):
            connection.execute(
                text(
                    """
                    INSERT INTO artifacts (
                        artifact_id, owner_id, tenant_id, task_id, run_id,
                        kind, filename, content_type, status,
                        idempotency_key, request_fingerprint
                    ) VALUES (
                        :artifact_id, 'owner-legacy', 'tenant-legacy',
                        'task-legacy', 'run-legacy', 'legacy_component',
                        :filename, 'application/octet-stream', 'available',
                        :idempotency_key, :request_fingerprint
                    )
                    """
                ),
                {
                    "artifact_id": f"artifact-{role}",
                    "filename": f"{role}.bin",
                    "idempotency_key": f"artifact-{role}-key",
                    "request_fingerprint": _DIGEST,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO artifact_versions (
                        version_id, owner_id, tenant_id, task_id, artifact_id,
                        version_no, storage_key, filename, content_type,
                        size_bytes, sha256, metadata_json, lineage_json
                    ) VALUES (
                        :version_id, 'owner-legacy', 'tenant-legacy',
                        'task-legacy', :artifact_id, 1, :storage_key, :filename,
                        'application/octet-stream', 1, :sha256,
                        '{}'::jsonb, '{}'::jsonb
                    )
                    """
                ),
                {
                    "artifact_id": f"artifact-{role}",
                    "version_id": f"version-{role}",
                    "filename": f"{role}.bin",
                    "storage_key": f"legacy/{role}",
                    "sha256": _DIGEST,
                },
            )


def _insert_catalog_event(
    engine: Engine,
    *,
    suffix: str,
    scope: str,
    revision: int,
) -> None:
    principal_id = None if scope == "task" else "principal-legacy"
    catalog_scope_key = "task-legacy" if scope == "task" else "principal-legacy"
    definition_id = f"component_definition_{suffix * 32}"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO component_catalog_events (
                    catalog_entry_id, owner_id, tenant_id, task_id,
                    principal_id, catalog_scope_key, catalog_revision,
                    definition_id, resource_ref, resource_kind, scope,
                    name, description, version, content_digest, availability,
                    artifact_id, artifact_version_id, artifact_version_no,
                    artifact_sha256, preview_artifact_id,
                    preview_artifact_version_id, preview_artifact_version_no,
                    preview_artifact_sha256, validation_artifact_id,
                    validation_artifact_version_id,
                    validation_artifact_version_no,
                    validation_artifact_sha256, ifc_type_class, ifc_type_guid,
                    authoring_kind, manifest_digest, recipe_digest,
                    preview_digest, validation_report_digest,
                    family_contract_json, builder_policy_digest,
                    canonical_parameters_json, parameter_digest,
                    variant_digest, ports_json, compatibility_json,
                    source_authorities_json, exact_child_variants_json,
                    requirement_id, idempotency_key, request_fingerprint
                ) VALUES (
                    :catalog_entry_id, 'owner-legacy', 'tenant-legacy',
                    'task-legacy', :principal_id, :catalog_scope_key, :revision,
                    :definition_id, :resource_ref, 'component_template', :scope,
                    :name, 'legacy fixture', :variant_digest, :content_digest,
                    'active', 'artifact-published', 'version-published', 1,
                    :digest, 'artifact-preview', 'version-preview', 1,
                    :digest, 'artifact-validation', 'version-validation', 1,
                    :digest, 'IfcFlowFitting', '0AAAAAAAAAAAAAAAAAAAAA',
                    'template', :digest, :digest, :digest, :digest,
                    '{}'::jsonb, :builder_policy_digest, '{}'::jsonb,
                    :parameter_digest, :variant_digest, '[]'::json,
                    '[]'::json, '[]'::jsonb, '[]'::jsonb,
                    'requirement-legacy', :idempotency_key, :digest
                )
                """
            ),
            {
                "catalog_entry_id": f"catalog-{suffix}",
                "principal_id": principal_id,
                "catalog_scope_key": catalog_scope_key,
                "revision": revision,
                "definition_id": definition_id,
                "resource_ref": f"legacy/resource/{suffix}",
                "scope": scope,
                "name": f"Legacy {suffix}",
                "variant_digest": _TAGGED_DIGEST,
                "content_digest": _DIGEST,
                "digest": _DIGEST,
                "builder_policy_digest": _TAGGED_DIGEST,
                "parameter_digest": _TAGGED_DIGEST,
                "idempotency_key": f"catalog-{suffix}-key",
            },
        )


def _seed_legacy_fixture(engine: Engine, table_name: str) -> dict[str, int]:
    _seed_identity_scope(engine)
    _seed_artifacts(engine)

    if table_name == "run_component_views":
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO run_component_views (
                        component_id, owner_id, tenant_id, task_id, run_id,
                        stage_key, requirement_id, component_name, status,
                        resource_ref, version, content_digest, catalog_revision,
                        ifc_type_class, authoring_kind, manifest_digest,
                        family_contract_digest, parameter_digest, variant_digest,
                        preview_digest, validation_report_digest,
                        published_artifact_id, published_artifact_version_id,
                        published_artifact_version_no, preview_artifact_id,
                        preview_artifact_version_id, preview_artifact_version_no,
                        validation_artifact_id, validation_artifact_version_id,
                        validation_artifact_version_no, receipt_artifact_id,
                        receipt_artifact_version_id, receipt_artifact_version_no,
                        reused, subject_refs, thread_id, source_checkpoint_id,
                        source_checkpoint_ns, source_root_checkpoint_id,
                        source_checkpoint_map_digest, created_by_agent, attempt,
                        projector_name, projector_version, output_fingerprint,
                        request_fingerprint
                    ) VALUES (
                        'component-legacy', 'owner-legacy', 'tenant-legacy',
                        'task-legacy', 'run-legacy', 'legacy-stage',
                        'requirement-legacy', 'Legacy component', 'published',
                        'legacy/resource', :variant_digest, :digest, 1,
                        'IfcFlowFitting', 'template', :digest,
                        :variant_digest, :variant_digest, :variant_digest,
                        :digest, :digest, 'artifact-published',
                        'version-published', 1, 'artifact-preview',
                        'version-preview', 1, 'artifact-validation',
                        'version-validation', 1, 'artifact-receipt',
                        'version-receipt', 1, false, '[]'::jsonb,
                        'thread-legacy', 'checkpoint-legacy', '',
                        'checkpoint-legacy', :digest, 'legacy-agent', 1,
                        'legacy-projector', '1', :digest, :digest
                    )
                    """
                ),
                {"variant_digest": _TAGGED_DIGEST, "digest": _DIGEST},
            )
    elif table_name == "component_catalog_events":
        _insert_catalog_event(engine, suffix="a", scope="task", revision=1)
    elif table_name == "component_publication_receipts":
        _insert_catalog_event(engine, suffix="a", scope="task", revision=1)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO component_publication_receipts (
                        publication_receipt_id, owner_id, tenant_id, task_id,
                        run_id, publication_scope, actor_principal_id,
                        catalog_scope_key, idempotency_key, request_fingerprint,
                        invocation_id, invocation_revision, invocation_digest,
                        catalog_proof_digest, session_id, first_writer_id,
                        first_writer_generation, catalog_entry_id,
                        result_catalog_revision, event_appended
                    ) VALUES (
                        'receipt-legacy', 'owner-legacy', 'tenant-legacy',
                        'task-legacy', 'run-legacy', 'task',
                        'principal-legacy', 'task-legacy', 'receipt-legacy-key',
                        :digest, 'invocation-legacy', 1, :digest, :digest,
                        'session-legacy', 'writer-legacy', 1, 'catalog-a', 1, true
                    )
                    """
                ),
                {"digest": _DIGEST},
            )
    elif table_name == "component_promotions":
        _insert_catalog_event(engine, suffix="a", scope="task", revision=1)
        _insert_catalog_event(engine, suffix="b", scope="user", revision=1)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO component_promotions (
                        promotion_id, owner_id, tenant_id, principal_id, task_id,
                        status, source_catalog_entry_id, source_definition_id,
                        source_component_version_id, source_manifest_digest,
                        target_catalog_entry_id, target_definition_id,
                        target_component_version_id, target_manifest_digest,
                        target_resource_ref, policy_digest, validation_digest,
                        receipt_digest, idempotency_key, request_fingerprint
                    ) VALUES (
                        'promotion-legacy', 'owner-legacy', 'tenant-legacy',
                        'principal-legacy', 'task-legacy', 'completed',
                        'catalog-a', :source_definition_id, 'version-source',
                        :digest, 'catalog-b', :target_definition_id,
                        'version-target', :digest, 'legacy/resource/b',
                        :digest, :digest, :digest, 'promotion-legacy-key', :digest
                    )
                    """
                ),
                {
                    "source_definition_id": f"component_definition_{'a' * 32}",
                    "target_definition_id": f"component_definition_{'b' * 32}",
                    "digest": _DIGEST,
                },
            )
    else:  # pragma: no cover - the parametrization is the closed fixture set
        raise AssertionError(f"unsupported legacy table fixture: {table_name}")

    return _legacy_counts(engine)


def test_fresh_database_upgrades_to_geometry_resource_head() -> None:
    with _isolated_database("geometry_fresh") as (config, engine):
        command.upgrade(config, "head")

        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == EXPECTED_SCHEMA_REVISION
            )
        assert _table_names(engine, _LEGACY_TABLES) == set()
        assert _table_names(engine, _GEOMETRY_TABLES) == set(_GEOMETRY_TABLES)


def test_empty_0064_database_upgrades_without_partial_state() -> None:
    with _isolated_database("geometry_from_0064") as (config, engine):
        command.upgrade(config, "20260830_0064")
        assert _legacy_counts(engine) == {table_name: 0 for table_name in _LEGACY_TABLES}

        command.upgrade(config, "head")

        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == EXPECTED_SCHEMA_REVISION
            )
        assert _table_names(engine, _LEGACY_TABLES) == set()
        assert _table_names(engine, _GEOMETRY_TABLES) == set(_GEOMETRY_TABLES)


def test_empty_0068_authoring_schema_roundtrips_through_0067() -> None:
    with _isolated_database("geometry_authoring_roundtrip") as (config, engine):
        command.upgrade(config, "20260902_0067")
        _assert_authoring_migration_state(
            engine,
            revision="20260902_0067",
            enabled=False,
        )

        command.upgrade(config, "20260902_0068")
        _assert_authoring_migration_state(
            engine,
            revision="20260902_0068",
            enabled=True,
        )

        command.downgrade(config, "20260902_0067")
        _assert_authoring_migration_state(
            engine,
            revision="20260902_0067",
            enabled=False,
        )

        command.upgrade(config, "20260902_0068")
        _assert_authoring_migration_state(
            engine,
            revision="20260902_0068",
            enabled=True,
        )


def _seed_annotation_base(engine: Engine) -> None:
    _seed_identity_scope(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO geometry_resources (
                    resource_id, resource_kind, owner_scope, owner_tenant_id,
                    owner_task_id, display_name
                ) VALUES (
                    'geometry-annotation-base', 'mapped_model', 'TASK',
                    'tenant-legacy', 'task-legacy', 'Annotation base'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    artifact_id, owner_id, tenant_id, task_id, run_id, kind,
                    filename, content_type, status, idempotency_key, request_fingerprint
                ) VALUES (
                    'annotation-forged-source', 'owner-legacy', 'tenant-legacy',
                    'task-legacy', 'run-legacy', 'geometry_authoring_source',
                    'source.zip', 'application/zip', 'available',
                    'annotation-forged-source-key', repeat('c', 64)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO artifact_versions (
                    version_id, owner_id, tenant_id, task_id, artifact_id,
                    version_no, storage_key, filename, content_type, size_bytes,
                    sha256, metadata_json, lineage_json
                ) VALUES (
                    'annotation-forged-source-v1', 'owner-legacy', 'tenant-legacy',
                    'task-legacy', 'annotation-forged-source', 1,
                    'annotation/forged/source', 'source.zip', 'application/zip', 1,
                    repeat('e', 64), '{}'::jsonb, '{}'::jsonb
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO geometry_resource_versions (
                    resource_version_id, resource_id, version_no, origin_kind,
                    content_digest, manifest_digest, manifest_json, lineage_json,
                    validation_summary_json
                ) VALUES (
                    'geometry-annotation-base-v1', 'geometry-annotation-base', 1,
                    'agent_generated', repeat('a', 64), repeat('b', 64),
                    jsonb_build_object('schema', 'geometry_authoring_manifest_v1'),
                    '{}'::jsonb, jsonb_build_object('valid', true)
                )
                """
            )
        )


def _insert_annotation_operation(
    engine: Engine,
    *,
    operation_id: str,
    digest: str,
    forged_code: bool = False,
) -> None:
    detail = {
        "detail_kind": "geometry_interface_annotation",
        "resource_kind": "mapped_model",
        "base_resource_id": "geometry-annotation-base",
        "base_resource_version_id": "geometry-annotation-base-v1",
        "base_content_digest": digest,
        "target_mode": "same_task_root",
        "display_name": "Annotation base",
        "interfaces": [],
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO critical_operations (
                    operation_id, owner_id, tenant_id, task_id, run_id,
                    operation_kind, initiator, tool_name, intent_summary,
                    idempotency_key, request_fingerprint, status,
                    code_artifact_id, code_artifact_version_id, code_artifact_sha256,
                    target_geometry_resource_id, operation_detail_json, started_at
                ) VALUES (
                    :operation_id, 'owner-legacy', 'tenant-legacy', 'task-legacy',
                    'run-legacy', 'geometry_resource_write', 'direct_client',
                    'ifc_geometry_resource_write',
                    'Replace exact fixed-model interfaces without rebuilding geometry.',
                    :idempotency_key, :request_fingerprint, 'prepared',
                    :code_artifact_id, :code_artifact_version_id, :code_artifact_sha256,
                    'geometry-annotation-base', CAST(:detail AS JSONB), CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "operation_id": operation_id,
                "idempotency_key": f"idempotency-{operation_id}",
                "request_fingerprint": ("c" if digest == _DIGEST else "d") * 64,
                "code_artifact_id": "annotation-forged-source" if forged_code else None,
                "code_artifact_version_id": (
                    "annotation-forged-source-v1" if forged_code else None
                ),
                "code_artifact_sha256": "e" * 64 if forged_code else None,
                "detail": json.dumps(detail),
            },
        )


def test_0077_annotation_guard_reauthorizes_exact_mapped_base_and_downgrade_fails_closed() -> None:
    with _isolated_database("geometry_interface_annotation") as (config, engine):
        command.upgrade(config, "20260905_0076")
        _seed_annotation_base(engine)
        command.upgrade(config, "20260905_0077")

        with pytest.raises(DBAPIError, match="exact mapped base is invalid"):
            _insert_annotation_operation(
                engine,
                operation_id="operation-annotation-wrong-base",
                digest="f" * 64,
            )

        with pytest.raises(DBAPIError):
            _insert_annotation_operation(
                engine,
                operation_id="operation-annotation-forged-code",
                digest=_DIGEST,
                forged_code=True,
            )

        _insert_annotation_operation(
            engine,
            operation_id="operation-annotation-valid",
            digest=_DIGEST,
        )
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT code_artifact_id, operation_detail_json ->> 'detail_kind' "
                    "FROM critical_operations "
                    "WHERE operation_id = 'operation-annotation-valid'"
                )
            ).one()
        assert row == (None, "geometry_interface_annotation")

        with pytest.raises(DBAPIError, match="annotation Operations exist"):
            command.downgrade(config, "20260905_0076")


def test_empty_0077_annotation_migration_roundtrips_through_0076() -> None:
    with _isolated_database("geometry_interface_annotation_empty") as (config, engine):
        command.upgrade(config, "20260905_0076")
        command.upgrade(config, "20260905_0077")
        command.downgrade(config, "20260905_0076")
        command.upgrade(config, "20260905_0077")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260905_0077"
            )


def test_0078_system_curator_schema_readiness_is_minimal_and_roundtrips() -> None:
    with _isolated_database("system_curator_schema_readiness") as (config, engine):
        command.upgrade(config, "20260905_0077")

        def privileges() -> dict[str, bool]:
            with engine.connect() as connection:
                relation = connection.execute(
                    text("SELECT current_schema() || '.alembic_version'")
                ).scalar_one()
                return {
                    privilege: connection.execute(
                        text(
                            "SELECT has_table_privilege("
                            "'cad2bim_system_curator', :relation, :privilege)"
                        ),
                        {"relation": relation, "privilege": privilege},
                    ).scalar_one()
                    for privilege in (
                        "SELECT",
                        "INSERT",
                        "UPDATE",
                        "DELETE",
                        "TRUNCATE",
                        "REFERENCES",
                        "TRIGGER",
                    )
                }

        assert privileges() == {
            "SELECT": False,
            "INSERT": False,
            "UPDATE": False,
            "DELETE": False,
            "TRUNCATE": False,
            "REFERENCES": False,
            "TRIGGER": False,
        }

        command.upgrade(config, "20260906_0078")
        assert privileges() == {
            "SELECT": True,
            "INSERT": False,
            "UPDATE": False,
            "DELETE": False,
            "TRUNCATE": False,
            "REFERENCES": False,
            "TRIGGER": False,
        }
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260906_0078"
            )
            assert (
                connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'cad2bim_system_curator', current_schema() || '.tasks', 'SELECT')"
                    )
                ).scalar_one()
                is False
            )

        command.downgrade(config, "20260905_0077")
        assert privileges()["SELECT"] is False

        command.upgrade(config, "20260906_0078")
        assert privileges()["SELECT"] is True


def test_empty_0069_query_schema_roundtrips_through_0068() -> None:
    with _isolated_database("ifc_query_roundtrip") as (config, engine):
        command.upgrade(config, "20260902_0068")
        assert _table_names(engine, _IFC_QUERY_TABLES) == set()

        command.upgrade(config, "20260903_0069")
        assert _table_names(engine, _IFC_QUERY_TABLES) == set(_IFC_QUERY_TABLES)

        command.downgrade(config, "20260902_0068")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260902_0068"
            )
        assert _table_names(engine, _IFC_QUERY_TABLES) == set()

        command.upgrade(config, "20260903_0069")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260903_0069"
            )
        assert _table_names(engine, _IFC_QUERY_TABLES) == set(_IFC_QUERY_TABLES)


def test_empty_0070_unified_geometry_tool_check_roundtrips_through_0069() -> None:
    with _isolated_database("geometry_write_tool_roundtrip") as (config, engine):
        command.upgrade(config, "20260903_0069")

        command.upgrade(config, "20260903_0070")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260903_0070"
            )
            unified_check = connection.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'critical_operations'::regclass "
                    "AND contype = 'c' AND pg_get_constraintdef(oid) "
                    "LIKE '%ifc_geometry_resource_write%'"
                )
            ).scalar_one()
            assert "ifc_geometry_resource_create" in unified_check
            assert "ifc_geometry_resource_repair" in unified_check

        command.downgrade(config, "20260903_0069")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260903_0069"
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_constraint "
                        "WHERE conrelid = 'critical_operations'::regclass "
                        "AND contype = 'c' AND pg_get_constraintdef(oid) "
                        "LIKE '%ifc_geometry_resource_write%'"
                    )
                ).scalar_one()
                == 0
            )

        command.upgrade(config, "20260903_0070")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260903_0070"
            )


def test_nonempty_0069_query_history_blocks_downgrade_atomically() -> None:
    with _isolated_database("ifc_query_nonempty") as (config, engine):
        command.upgrade(config, "20260903_0069")
        _seed_identity_scope(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO artifacts (
                        artifact_id, owner_id, tenant_id, task_id, run_id,
                        kind, filename, content_type, status, current_version_id,
                        idempotency_key, request_fingerprint
                    ) VALUES (
                        'artifact-ifc-query-source', 'owner-legacy', 'tenant-legacy',
                        'task-legacy', 'run-legacy', 'ifc_query_source',
                        'ifc_query.py', 'text/x-python', 'available', NULL,
                        'ifc-query-source-key', repeat('d', 64)
                    )
                    """
                )
            )

        with pytest.raises(DBAPIError) as exc_info:
            command.downgrade(config, "20260902_0068")

        assert "IFC query history is not empty" in str(exc_info.value)
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260903_0069"
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM artifacts "
                        "WHERE artifact_id = 'artifact-ifc-query-source'"
                    )
                ).scalar_one()
                == 1
            )
        assert _table_names(engine, _IFC_QUERY_TABLES) == set(_IFC_QUERY_TABLES)


@pytest.mark.parametrize("populated_table", _LEGACY_TABLES)
def test_legacy_product_core_data_fails_closed_at_0064(populated_table: str) -> None:
    with _isolated_database(f"geometry_legacy_{populated_table}") as (config, engine):
        command.upgrade(config, "20260830_0064")
        expected_counts = _seed_legacy_fixture(engine, populated_table)
        expected_message = (
            "legacy Product-Core tables are not empty: "
            f"run_component_views={expected_counts['run_component_views']}, "
            "component_catalog_events="
            f"{expected_counts['component_catalog_events']}, "
            "component_publication_receipts="
            f"{expected_counts['component_publication_receipts']}, "
            f"component_promotions={expected_counts['component_promotions']}"
        )

        with pytest.raises(DBAPIError) as exc_info:
            command.upgrade(config, "head")

        assert expected_message in str(exc_info.value)
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260830_0064"
            )
        assert _table_names(engine, _LEGACY_TABLES) == set(_LEGACY_TABLES)
        assert _legacy_counts(engine) == expected_counts
        assert _table_names(engine, _GEOMETRY_TABLES) == set()
