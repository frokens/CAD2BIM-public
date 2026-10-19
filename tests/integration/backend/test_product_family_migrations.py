from __future__ import annotations

from CAD2BIM_backend.infrastructure.postgres.database import EXPECTED_SCHEMA_REVISION
from CAD2BIM_backend.domain.product_family import ProductFamilyContract

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
import rfc8785
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import DBAPIError


pytestmark = [pytest.mark.integration, pytest.mark.network]

TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()
_PREVIOUS_REVISION = "20260904_0072"
_PRODUCT_FAMILY_REVISION = "20260904_0073"
_AUTHORING_REVISION = "20260904_0074"
_GEOMETRY_QUERY_REVISION = "20260905_0075"
_HISTORICAL_HEAD_REVISION = "20260913_0085"
_HEAD_REVISION = EXPECTED_SCHEMA_REVISION
_DIGEST = "a" * 64
_USAGE = """# Inputs
No runtime inputs.
# Outputs
Returns the declared product assembly.
# Datums and Interfaces
Use the declared family interfaces.
# Allowed Operations
Placement and connection only.
# Forbidden Operations
Do not scale, cut, or mutate the family geometry.
# Validation
Verify the declared component and interface contract.
"""
_PRODUCT_FAMILY_TABLES = (
    "product_families",
    "product_family_versions",
    "product_family_geometry_bindings",
    "product_family_version_assets",
)
_LEGACY_PRODUCT_CORE_TABLES = (
    "run_component_views",
    "component_catalog_events",
    "component_publication_receipts",
    "component_promotions",
)


def _content_digest(*, abi_profile: str | None) -> str:
    document: dict[str, object] = {
        "domain": "product_family_version_content",
        "contract_digest": "c" * 64,
        "program_entry": "main.py:realize",
        "runtime_profile": "product_realization_v1",
        "budget_profile": "product_realization_default",
        "geometry_bindings": [
            {
                "binding_key": "body",
                "geometry_resource_id": "geometry-abi",
                "geometry_resource_version_id": "geometry-abi-v1",
                "geometry_content_digest": "a" * 64,
                "expected_resource_kind": "mapped_model",
                "geometry_purpose": "product_component",
            }
        ],
        "assets": [
            {
                "asset_role": "realization_source_bundle",
                "asset_format": "source_bundle_v1",
                "filename": "source.zip",
                "content_type": "application/zip",
                "size_bytes": 1,
                "sha256": "e" * 64,
            }
        ],
    }
    if abi_profile is not None:
        document["abi_profile"] = abi_profile
    return hashlib.sha256(rfc8785.dumps(document)).hexdigest()


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


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _column_names(engine: Engine, table_name: str) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :table_name"
                ),
                {"table_name": table_name},
            ).scalars()
        )


def _row_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
            for table in _PRODUCT_FAMILY_TABLES
        }


def _seed_product_family_repair_scope(engine: Engine) -> dict[str, str]:
    suffix = uuid4().hex[:12]
    values = {
        "principal_id": f"principal-repair-{suffix}",
        "tenant_id": f"tenant-repair-{suffix}",
        "owner_id": f"owner-repair-{suffix}",
        "task_id": f"task-repair-{suffix}",
        "run_id": f"run-repair-{suffix}",
        "run_key": f"run-key-repair-{suffix}",
        "thread_id": f"thread-repair-{suffix}",
        "code_artifact_id": f"source-repair-{suffix}",
        "code_version_id": f"source-version-repair-{suffix}",
    }
    with engine.begin() as connection:
        statements = (
            "INSERT INTO principals (principal_id, issuer, subject, status) "
            "VALUES (:principal_id, 'test-issuer', :owner_id, 'active')",
            "INSERT INTO tenants (tenant_id, kind, status) "
            "VALUES (:tenant_id, 'personal', 'active')",
            "INSERT INTO tenant_principals (tenant_id, principal_id, access) "
            "VALUES (:tenant_id, :principal_id, 'owner')",
            "INSERT INTO users (owner_id, tenant_id) VALUES (:owner_id, :tenant_id)",
            "INSERT INTO tasks (task_id, owner_id, tenant_id, title, metadata_json, "
            "status, active_module) VALUES (:task_id, :owner_id, :tenant_id, "
            "'ProductFamily repair fence fixture', '{}'::jsonb, 'running', 'data_runtime')",
            "INSERT INTO runs (run_id, owner_id, tenant_id, task_id, module, stage_key, "
            "input_payload, idempotency_key, request_fingerprint, thread_id, status, "
            "execution_protocol_status, execution_mode) VALUES (:run_id, :owner_id, "
            ":tenant_id, :task_id, 'data_runtime', 'direct', '{}'::jsonb, :run_key, "
            "repeat('b', 64), :thread_id, 'running', 'not_applicable', 'direct_mcp')",
        )
        for statement in statements:
            connection.execute(text(statement), values)
        connection.execute(
            text(
                """
                INSERT INTO artifacts (
                    artifact_id, owner_id, tenant_id, task_id, run_id,
                    kind, filename, content_type, status,
                    idempotency_key, request_fingerprint
                ) VALUES (
                    :code_artifact_id, :owner_id, :tenant_id, :task_id, :run_id,
                    'product_family_authoring_source', 'source.zip',
                    'application/zip', 'available', :run_key, repeat('c', 64)
                )
                """
            ),
            values,
        )
        connection.execute(
            text(
                """
                INSERT INTO artifact_versions (
                    version_id, owner_id, tenant_id, task_id, artifact_id,
                    version_no, storage_key, filename, content_type,
                    size_bytes, sha256, metadata_json, lineage_json
                ) VALUES (
                    :code_version_id, :owner_id, :tenant_id, :task_id,
                    :code_artifact_id, 1, :code_storage_key, 'source.zip',
                    'application/zip', 1, :code_sha256, '{}'::jsonb, '{}'::jsonb
                )
                """
            ),
            {
                **values,
                "code_storage_key": values["code_version_id"],
                "code_sha256": _DIGEST,
            },
        )
    return values


def _insert_product_family_operation(
    connection: Connection,
    values: dict[str, str],
    *,
    operation_id: str,
    mode: str,
    version_draft: dict[str, object],
    repair_parent_operation_id: str | None = None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO critical_operations (
                operation_id, owner_id, tenant_id, task_id, run_id,
                operation_kind, initiator, tool_name, intent_summary,
                idempotency_key, request_fingerprint, status,
                code_artifact_id, code_artifact_version_id, code_artifact_sha256,
                repair_parent_operation_id, target_product_family_id,
                operation_detail_json, started_at
            ) VALUES (
                :operation_id, :owner_id, :tenant_id, :task_id, :run_id,
                'product_family_write', 'direct_client', 'ifc_product_family_write',
                'ProductFamily repair fence migration fixture',
                :idempotency_key, :request_fingerprint, 'prepared',
                :code_artifact_id, :code_version_id, :code_sha256,
                :repair_parent_operation_id, :target_product_family_id,
                CAST(:operation_detail_json AS JSONB), CURRENT_TIMESTAMP
            )
            """
        ),
        {
            **values,
            "operation_id": operation_id,
            "idempotency_key": f"idempotency-{operation_id}",
            "request_fingerprint": hashlib.sha256(operation_id.encode()).hexdigest(),
            "code_sha256": _DIGEST,
            "repair_parent_operation_id": repair_parent_operation_id,
            "target_product_family_id": "family-repair",
            "operation_detail_json": json.dumps(
                {
                    "detail_kind": "product_family_write",
                    "mode": mode,
                    "version_draft": version_draft,
                }
            ),
        },
    )


def test_fresh_database_upgrades_to_product_family_head_without_legacy_product_core() -> None:
    with _isolated_database("product_family_fresh") as (config, engine):
        command.upgrade(config, "head")

        assert _revision(engine) == _HEAD_REVISION
        assert _table_names(engine, _PRODUCT_FAMILY_TABLES) == set(_PRODUCT_FAMILY_TABLES)
        assert _table_names(engine, _LEGACY_PRODUCT_CORE_TABLES) == set()
        family_columns = _column_names(engine, "product_families")
        assert (
            not {
                "current_family_version_id",
                "default_family_version_id",
                "latest_family_version_id",
            }
            & family_columns
        )
        with engine.connect() as connection:
            geometry_guard = connection.execute(
                text(
                    "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname = "
                    "'cad2bim_guard_geometry_authoring_operation' AND pronamespace = "
                    "(SELECT oid FROM pg_namespace WHERE nspname = current_schema())"
                )
            ).scalar_one()
            annotation_usage_constraint = connection.execute(
                text(
                    "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
                    "WHERE conname = "
                    "'ck_critical_operations_geometry_annotation_usage_current' "
                    "AND connamespace = current_schema()::regnamespace"
                )
            ).one()
        assert "result_version.manifest_json -> 'usage_instructions'" in geometry_guard
        assert "NEW.operation_detail_json -> 'usage_instructions'" in geometry_guard
        assert "- 'interfaces') - 'usage_instructions'" in geometry_guard
        assert "geometry_interface_annotation" in annotation_usage_constraint[0]
        assert "usage_instructions" in annotation_usage_constraint[0]
        assert annotation_usage_constraint[1] is False


def test_0090_admits_current_family_operation_and_keeps_historical_facts() -> None:
    with _isolated_database("family_binding_options") as (config, engine):
        command.upgrade(config, "20260924_0089")
        values = _seed_product_family_repair_scope(engine)
        draft = {
            "draft_schema": "product_family_authoring_draft_v2",
            "family_id": "family-repair",
            "family_version_id": "family-repair-v1",
            "abi_profile": "product_realization_v3",
            "family_contract": {
                "schema": "product_family_contract_v3",
                "standard_model": {},
            },
            "usage_instructions": _USAGE,
        }
        with engine.begin() as connection:
            _insert_product_family_operation(
                connection, values, operation_id="historical-v3", mode="create",
                version_draft=draft,
            )
            historical_before = connection.execute(text(
                "SELECT to_jsonb(operation) FROM critical_operations operation "
                "WHERE operation_id = 'historical-v3'"
            )).scalar_one()

        # The v4 admission and rollback fence belong to 0090. Later migrations
        # retire format generations and do not offer a downgrade through 0090.
        command.upgrade(config, "20260927_0090")
        assert _revision(engine) == "20260927_0090"
        with engine.connect() as connection:
            definitions = {
                name: connection.execute(text(
                    "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname = :name "
                    "AND pronamespace = current_schema()::regnamespace"
                ), {"name": name}).scalar_one()
                for name in (
                    "cad2bim_guard_product_family_current_version",
                    "cad2bim_guard_product_family_current_operation",
                    "cad2bim_guard_product_family_authoring_operation",
                )
            }
            version_check = connection.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_product_family_versions_contract_shape' "
                "AND connamespace = current_schema()::regnamespace"
            )).scalar_one()
            retained = connection.execute(text(
                "SELECT operation_detail_json #>> '{version_draft,abi_profile}' "
                "FROM critical_operations WHERE operation_id = 'historical-v3'"
            )).scalar_one()
            assert connection.execute(text(
                "SELECT to_jsonb(operation) FROM critical_operations operation "
                "WHERE operation_id = 'historical-v3'"
            )).scalar_one() == historical_before
        assert retained == "product_realization_v3"
        assert "product_family_contract_v4" in version_check
        for definition in definitions.values():
            assert "product_family_contract_v4" in definition
            assert "product_realization_v4" in definition

        current = {
            **draft,
            "family_version_id": "family-repair-v2",
            "abi_profile": "product_realization_v4",
            "family_contract": {
                "schema": "product_family_contract_v4",
                "standard_model": {},
            },
        }
        with engine.begin() as connection:
            _insert_product_family_operation(
                connection, values, operation_id="current-v4", mode="create",
                version_draft=current,
            )
        with pytest.raises(DBAPIError) as error:
            with engine.begin() as connection:
                _insert_product_family_operation(
                    connection, values, operation_id="new-v3", mode="create",
                    version_draft=draft,
                )
        assert getattr(error.value.orig, "sqlstate", None) == "23514"
        history_query = text(
            "SELECT operation_id, to_jsonb(operation) FROM critical_operations operation "
            "ORDER BY operation_id"
        )
        with engine.connect() as connection:
            before_downgrade = dict(connection.execute(history_query).all())
        assert set(before_downgrade) == {"historical-v3", "current-v4"}
        with pytest.raises(DBAPIError, match="Cannot downgrade retained binding-option"):
            command.downgrade(config, "20260924_0089")
        assert _revision(engine) == "20260927_0090"
        with engine.connect() as connection:
            assert dict(connection.execute(history_query).all()) == before_downgrade

        command.upgrade(config, _HEAD_REVISION)
        assert _revision(engine) == _HEAD_REVISION
        with engine.connect() as connection:
            after_head = dict(connection.execute(history_query).all())
        assert set(after_head) == set(before_downgrade)
        for operation_id, original in before_downgrade.items():
            # Compare every field that existed at 0090, including raw detail,
            # identity, kind, status and result/error bindings. New nullable
            # execution fields at head are not historical source mutations.
            for field, value in original.items():
                assert after_head[operation_id][field] == value, (operation_id, field)


def test_0104_current_family_cutover_preserves_v4_and_geometry_admission() -> None:
    definition = json.loads((Path(__file__).resolve().parents[2] /
        "scenarios/engineering_product_families/families/bend/definition.json").read_text())
    contract = ProductFamilyContract.model_validate(definition).model_dump(mode="json", by_alias=True)
    insert = text("""
        INSERT INTO product_family_versions (
            family_version_id, family_id, version_no, display_name, purpose,
            family_contract_json, contract_digest, content_digest, program_entry,
            runtime_profile, budget_profile, validation_summary_json,
            abi_profile, usage_instructions
        ) VALUES (
            'family-cutover-v1', 'family-cutover', 1, 'Current Family', 'Admission probe',
            CAST(:contract AS jsonb), repeat('c',64), repeat('d',64), 'main.py:realize',
            'product_realization_python_v1', 'product_realization_budget_v1',
            jsonb_build_object('valid',true), :abi, :usage
        )
    """)
    with _isolated_database("family_v5_cutover") as (config, engine):
        command.upgrade(config, "20260927_0090")
        values = _seed_product_family_repair_scope(engine)
        old = {
            "draft_schema": "product_family_authoring_draft_v2",
            "family_id": "family-repair", "family_version_id": "family-repair-v1",
            "abi_profile": "product_realization_v4",
            "family_contract": {"schema": "product_family_contract_v4", "standard_model": {}},
            "usage_instructions": _USAGE,
        }
        with engine.begin() as connection:
            _insert_product_family_operation(connection, values, operation_id="retained-v4",
                mode="create", version_draft=old)
            connection.execute(text("INSERT INTO product_families (family_id,owner_scope) "
                "VALUES ('family-cutover','SYSTEM')"))
        command.upgrade(config, "20261001_0103")
        with engine.connect() as connection:
            before = connection.scalar(text("SELECT operation_detail_json FROM critical_operations "
                "WHERE operation_id='retained-v4'"))
            geometry_before = connection.scalar(text("SELECT pg_get_functiondef("
                "'cad2bim_guard_geometry_query_attempt()'::regprocedure)"))
        command.upgrade(config, "20261001_0104")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT operation_detail_json FROM critical_operations "
                "WHERE operation_id='retained-v4'")) == before
            operation_check, validated = connection.execute(text(
                "SELECT pg_get_constraintdef(oid),convalidated FROM pg_constraint "
                "WHERE conname='ck_critical_operations_operation_kind_detail_valid' "
                "AND connamespace=current_schema()::regnamespace")).one()
            assert validated is False
            for current_branch in ("geometry_inspection", "authoring_draft_write", "authoring_publish",
                "preview_examples", "system_copy", "product_family_contract_v5", "product_realization_v5"):
                assert current_branch in operation_check
            geometry_after = connection.scalar(text("SELECT pg_get_functiondef("
                "'cad2bim_guard_geometry_query_attempt()'::regprocedure)"))
            assert geometry_after == geometry_before.replace("product_family_contract_v4",
                "product_family_contract_v5").replace("product_realization_v4", "product_realization_v5")
        # These rollback-only probes exercise the immediate current admission
        # guard; real asset/binding publication is covered by the draft slice.
        for current in (True, False):
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    parameters = {"contract": json.dumps({**contract, "schema": "product_family_contract_v5"} if current else
                        {**contract, "schema": "product_family_contract_v4"}),
                        "abi": "product_realization_v5" if current else "product_realization_v4",
                        "usage": _USAGE}
                    if current:
                        connection.execute(insert, parameters)
                    else:
                        with pytest.raises(DBAPIError) as error:
                            connection.execute(insert, parameters)
                        assert getattr(error.value.orig, "sqlstate", None) == "23514"
                finally:
                    transaction.rollback()


def test_0105_removes_format_generations_without_rewriting_saved_operations() -> None:
    with _isolated_database("authoring_cleanup") as (config, engine):
        command.upgrade(config, "20260927_0090")
        values = _seed_product_family_repair_scope(engine)
        original = {
            "draft_schema": "product_family_authoring_draft_v2",
            "family_id": "family-repair", "family_version_id": "family-repair-v1",
            "abi_profile": "product_realization_v4",
            "family_contract": {"schema": "product_family_contract_v4", "standard_model": {}},
            "usage_instructions": _USAGE,
        }
        with engine.begin() as connection:
            _insert_product_family_operation(connection, values, operation_id="saved-original",
                mode="create", version_draft=original)
        command.upgrade(config, "20261001_0104")
        with engine.connect() as connection:
            before = connection.scalar(text("SELECT operation_detail_json FROM critical_operations "
                "WHERE operation_id='saved-original'"))
        command.upgrade(config, "20261001_0105")
        assert not {"abi_profile", "runtime_profile", "budget_profile"} & _column_names(engine, "product_family_versions")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT operation_detail_json FROM critical_operations "
                "WHERE operation_id='saved-original'")) == before
            for name in ("cad2bim_guard_product_family_current_version",
                         "cad2bim_guard_product_family_current_operation",
                         "cad2bim_guard_product_family_authoring_operation",
                         "cad2bim_guard_geometry_query_attempt",
                         "cad2bim_guard_geometry_inspection_operation",
                         "cad2bim_guard_geometry_formal_source"):
                definition = connection.scalar(text("SELECT pg_get_functiondef(CAST(:name AS regprocedure))"),
                    {"name": name + "()"})
                assert "product_family_contract_v" not in definition
                assert "product_realization_v" not in definition
                assert "metadata_schema" not in definition
                assert "geometry_inspection_v1" not in definition
            check = connection.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_critical_operations_operation_kind_detail_valid' "
                "AND connamespace=current_schema()::regnamespace"))
            assert "product_realization_v" not in check
            assert all(kind in check for kind in ("authoring_draft_write", "authoring_publish", "geometry_inspection", "system_copy"))


def test_0102_removes_only_obsolete_standard_model_admission() -> None:
    definition = json.loads((Path(__file__).resolve().parents[2] /
        "scenarios/engineering_product_families/families/bend/definition.json").read_text())
    with_parameters = ProductFamilyContract.model_validate({
        **definition,
        "standard_model": {"public_parameters": {"side_a_outer_diameter_m": 0.1}},
    }).model_dump(mode="json", by_alias=True)
    without_standard = ProductFamilyContract.model_validate({
        key: value for key, value in definition.items() if key != "standard_model"
    }).model_dump(mode="json", by_alias=True)
    # This migration test inserts historical storage directly. It does not send
    # the retired contract through any current DTO or online interpreter.
    with_parameters["schema"] = "product_family_contract_v4"
    without_standard["schema"] = "product_family_contract_v4"
    insert_version = text("""
        INSERT INTO product_family_versions (
            family_version_id, family_id, version_no, display_name, purpose,
            family_contract_json, contract_digest, content_digest, program_entry,
            runtime_profile, budget_profile, validation_summary_json,
            abi_profile, usage_instructions
        ) VALUES (
            'family-0102-v1', 'family-0102', 1, 'Current Family', 'Admission probe',
            CAST(:contract AS jsonb), repeat('c', 64), repeat('d', 64), 'main.py:realize',
            'product_realization_v1', 'product_realization_default',
            CAST(:summary AS jsonb), :abi, :usage
        )
    """)

    def attempt(engine: Engine, *, contract: dict, abi: str = "product_realization_v4",
                usage: str | None = _USAGE, summary: dict | None = None,
                admitted: bool) -> None:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                parameters = {"contract": json.dumps(contract), "abi": abi, "usage": usage,
                              "summary": json.dumps({"valid": True} if summary is None else summary)}
                if admitted:
                    connection.execute(insert_version, parameters)
                    assert connection.scalar(text(
                        "SELECT count(*) FROM product_family_versions WHERE family_id='family-0102'"
                    )) == 1
                else:
                    with pytest.raises(DBAPIError) as error:
                        connection.execute(insert_version, parameters)
                    assert getattr(error.value.orig, "sqlstate", None) == "23514"
            finally:
                transaction.rollback()

    with _isolated_database("family_standard_admission") as (config, engine):
        command.upgrade(config, "20261001_0101")
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO product_families (family_id, owner_scope)
                VALUES ('family-0102', 'SYSTEM')
            """))
        attempt(engine, contract=with_parameters, admitted=False)
        command.upgrade(config, "20261001_0102")
        assert _revision(engine) == "20261001_0102"
        with engine.connect() as connection:
            operation_guard = connection.execute(text("""
                SELECT pg_get_functiondef('cad2bim_guard_product_family_current_operation()'::regprocedure)
            """)).scalar_one()
        assert "{version_draft,family_contract,standard_model}' IS DISTINCT FROM '{}'::jsonb" not in operation_guard
        assert "product_family_contract_v4" in operation_guard
        assert "product_realization_v4" in operation_guard
        assert "validation_scenarios" in operation_guard

        attempt(engine, contract=with_parameters, admitted=True)
        attempt(engine, contract=without_standard, admitted=True)
        attempt(engine, contract={**with_parameters, "schema": "product_family_contract_v3"},
                admitted=False)
        attempt(engine, contract=with_parameters, abi="product_realization_v3", admitted=False)
        attempt(engine, contract=with_parameters, usage=None, admitted=False)
        attempt(engine, contract=with_parameters, summary={}, admitted=False)

        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(insert_version, {
                    "contract": json.dumps(with_parameters), "abi": "product_realization_v4",
                    "usage": _USAGE, "summary": '{"valid": true}',
                })
                with pytest.raises(DBAPIError, match="version is incomplete"):
                    connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            finally:
                transaction.rollback()


def test_0103_publish_retry_index_preserves_other_operation_uniqueness() -> None:
    publish_insert = text("""
        INSERT INTO critical_operations (
            operation_id, owner_id, tenant_id, task_id, run_id,
            operation_kind, initiator, tool_name, intent_summary,
            idempotency_key, request_fingerprint, status,
            target_product_family_id, operation_detail_json,
            started_at, finished_at, error_code, error_message, error_phase
        ) VALUES (
            :operation_id, :owner_id, :tenant_id, :task_id, :run_id,
            'authoring_publish', 'direct_client', 'ifc_authoring_publish',
            'Publish retry index migration fixture',
            :idempotency_key, :fingerprint, 'failed',
            'family-publish-0103', CAST(:detail AS jsonb),
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
            'AUTHORING_PUBLICATION_COMMIT_REJECTED', 'Publication failed', 'commit_fence'
        )
    """)
    source_insert = text("""
        INSERT INTO critical_operations (
            operation_id, owner_id, tenant_id, task_id, run_id,
            operation_kind, initiator, tool_name, intent_summary,
            idempotency_key, request_fingerprint, status,
            source_filename, operation_detail_json, started_at
        ) VALUES (
            :operation_id, :owner_id, :tenant_id, :task_id, :run_id,
            'source_ingestion', 'direct_client', 'cad_import_source',
            'Source index migration fixture',
            :idempotency_key, :fingerprint, 'prepared',
            'source.dxf', '{}'::jsonb, CURRENT_TIMESTAMP
        )
    """)

    def insert(connection: Connection, statement, values, identity: str,
               fingerprint: str = _DIGEST, idempotency_identity: str | None = None) -> None:
        connection.execute(statement, {
            **values, "operation_id": f"operation-{identity}",
            "idempotency_key": f"authoring-publish:{idempotency_identity or identity}",
            "fingerprint": fingerprint,
            "detail": json.dumps({
                "detail_kind": "authoring_publish", "root_kind": "family",
                "root_id": "family-publish-0103",
                "expected_drafts": [{"root_kind": "family", "root_id": "family-publish-0103"}],
                "preview_examples": [], "geometry_versions": [], "family_version": None,
            }),
        })

    def rejects_duplicate(connection: Connection, statement, values, identity: str,
                          constraint: str, fingerprint: str = _DIGEST,
                          idempotency_identity: str | None = None) -> None:
        with pytest.raises(DBAPIError) as error, connection.begin_nested():
            insert(connection, statement, values, identity, fingerprint,
                   idempotency_identity)
        assert getattr(error.value.orig, "sqlstate", None) == "23505"
        assert error.value.orig.diag.constraint_name == constraint

    with _isolated_database("authoring_publish_index") as (config, engine):
        command.upgrade(config, "20261001_0102")
        values = _seed_product_family_repair_scope(engine)
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO product_families (family_id, owner_scope)
                VALUES ('family-publish-0103', 'SYSTEM')
            """))
            # This migration test isolates the index from the publication admission
            # trigger; the Application integration test exercises that guard.
            connection.execute(text("""
                ALTER TABLE critical_operations
                DISABLE TRIGGER trg_authoring_publication_operation_guard
            """))
        with engine.begin() as connection:
            insert(connection, publish_insert, values, "original-request")
            rejects_duplicate(connection, publish_insert, values, "new-request",
                              "uq_critical_operations_run_tool_fingerprint")
        with engine.begin() as connection:
            connection.execute(text("""
                ALTER TABLE critical_operations
                ENABLE TRIGGER trg_authoring_publication_operation_guard
            """))

        command.upgrade(config, "20261001_0103")
        assert _revision(engine) == "20261001_0103"
        with engine.begin() as connection:
            assert connection.execute(text("""
                SELECT operation_id, status, request_fingerprint
                FROM critical_operations WHERE operation_id='operation-original-request'
            """)).one() == ("operation-original-request", "failed", _DIGEST)
            predicate = connection.scalar(text("""
                SELECT pg_get_expr(i.indpred, i.indrelid)
                FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
                WHERE c.relname='uq_critical_operations_run_tool_fingerprint'
                  AND c.relnamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema())
            """))
            assert predicate is not None
            assert "authoring_publish" not in predicate
            assert all(kind in predicate for kind in (
                "source_ingestion", "data_transform", "geometry_resource_write",
                "product_family_write",
            ))

        with engine.begin() as connection:
            connection.execute(text("""
                ALTER TABLE critical_operations
                DISABLE TRIGGER trg_authoring_publication_operation_guard
            """))
        with engine.begin() as connection:
            insert(connection, publish_insert, values, "new-request")
            rejects_duplicate(connection, publish_insert, values, "another-request",
                              "uq_critical_operations_run_idempotency", "b" * 64,
                              idempotency_identity="new-request")
        with engine.begin() as connection:
            connection.execute(text("""
                ALTER TABLE critical_operations
                ENABLE TRIGGER trg_authoring_publication_operation_guard
            """))
        with engine.begin() as connection:
            insert(connection, source_insert, values, "source-first")
            rejects_duplicate(connection, source_insert, values, "source-second",
                              "uq_critical_operations_run_tool_fingerprint")

        with pytest.raises(RuntimeError, match="cannot admit existing publication retries"):
            command.downgrade(config, "20261001_0102")
        assert _revision(engine) == "20261001_0103"


def test_empty_standard_assets_migration_roundtrips_without_new_tables() -> None:
    with _isolated_database("product_family_standard_empty") as (config, engine):
        # Exercise 0080's empty-asset reversibility at its own boundary; current
        # head intentionally cannot downgrade across the authoring cutover.
        command.upgrade(config, "20260909_0080")
        command.downgrade(config, "20260907_0079")
        assert _revision(engine) == "20260907_0079"
        command.upgrade(config, "20260909_0080")
        assert _revision(engine) == "20260909_0080"
        with engine.connect() as connection:
            guard = connection.execute(text(
                "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname = "
                "'cad2bim_guard_product_family_version_complete' AND pronamespace = "
                "(SELECT oid FROM pg_namespace WHERE nspname = current_schema())"
            )).scalar_one()
        assert "standard_model_manifest" in guard
        assert _table_names(engine, _PRODUCT_FAMILY_TABLES) == set(_PRODUCT_FAMILY_TABLES)
        command.upgrade(config, _HEAD_REVISION)
        assert _revision(engine) == _HEAD_REVISION
        assert _table_names(engine, _PRODUCT_FAMILY_TABLES) == set(_PRODUCT_FAMILY_TABLES)


def test_0072_upgrades_and_empty_0073_downgrade_reupgrade_roundtrips() -> None:
    with _isolated_database("product_family_roundtrip") as (config, engine):
        command.upgrade(config, _PREVIOUS_REVISION)
        assert _table_names(engine, _PRODUCT_FAMILY_TABLES) == set()
        assert _table_names(engine, _LEGACY_PRODUCT_CORE_TABLES) == set()

        command.upgrade(config, _PRODUCT_FAMILY_REVISION)
        assert _revision(engine) == _PRODUCT_FAMILY_REVISION
        assert _table_names(engine, _PRODUCT_FAMILY_TABLES) == set(_PRODUCT_FAMILY_TABLES)

        command.downgrade(config, _PREVIOUS_REVISION)
        assert _revision(engine) == _PREVIOUS_REVISION
        assert _table_names(engine, _PRODUCT_FAMILY_TABLES) == set()
        assert _table_names(engine, _LEGACY_PRODUCT_CORE_TABLES) == set()

        command.upgrade(config, _PRODUCT_FAMILY_REVISION)
        assert _revision(engine) == _PRODUCT_FAMILY_REVISION
        assert _table_names(engine, _PRODUCT_FAMILY_TABLES) == set(_PRODUCT_FAMILY_TABLES)
        assert _table_names(engine, _LEGACY_PRODUCT_CORE_TABLES) == set()


def test_empty_0074_authoring_migration_roundtrips_through_0073() -> None:
    with _isolated_database("product_family_authoring_roundtrip") as (config, engine):
        command.upgrade(config, _PRODUCT_FAMILY_REVISION)
        assert not {
            "target_product_family_id",
            "base_product_family_version_id",
            "result_product_family_version_id",
        } & _column_names(engine, "critical_operations")

        command.upgrade(config, _AUTHORING_REVISION)
        assert _revision(engine) == _AUTHORING_REVISION
        assert {
            "target_product_family_id",
            "base_product_family_version_id",
            "result_product_family_version_id",
        } <= _column_names(engine, "critical_operations")
        with engine.connect() as connection:
            insert_policies = int(
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_policies "
                        "WHERE schemaname = current_schema() "
                        "AND tablename = ANY(:tables) AND cmd = 'INSERT'"
                    ),
                    {"tables": list(_PRODUCT_FAMILY_TABLES)},
                ).scalar_one()
            )
        assert insert_policies == 4

        command.downgrade(config, _PRODUCT_FAMILY_REVISION)
        assert _revision(engine) == _PRODUCT_FAMILY_REVISION
        assert not {
            "target_product_family_id",
            "base_product_family_version_id",
            "result_product_family_version_id",
        } & _column_names(engine, "critical_operations")

        command.upgrade(config, _AUTHORING_REVISION)
        assert _revision(engine) == _AUTHORING_REVISION


def test_nonempty_0073_version_digest_roundtrips_through_0074_abi_profile() -> None:
    with _isolated_database("product_family_abi_digest") as (config, engine):
        command.upgrade(config, _PRODUCT_FAMILY_REVISION)
        old_digest = _content_digest(abi_profile=None)
        new_digest = _content_digest(abi_profile="product_realization_v1")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO geometry_resources ("
                    "resource_id, resource_kind, owner_scope, owner_tenant_id, "
                    "owner_task_id, display_name"
                    ") VALUES ('geometry-abi', 'mapped_model', 'SYSTEM', NULL, NULL, "
                    "'ABI geometry')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO geometry_resource_versions ("
                    "resource_version_id, resource_id, version_no, origin_kind, "
                    "content_digest, manifest_digest, manifest_json, lineage_json, "
                    "validation_summary_json"
                    ") VALUES ('geometry-abi-v1', 'geometry-abi', 1, 'trusted_seed', "
                    "repeat('a', 64), repeat('b', 64), '{}'::jsonb, '{}'::jsonb, "
                    "'{\"valid\": true}'::jsonb)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO product_families ("
                    "family_id, owner_scope, owner_tenant_id, owner_task_id"
                    ") VALUES ('family-abi', 'SYSTEM', NULL, NULL)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO product_family_versions ("
                    "family_version_id, family_id, version_no, display_name, purpose, "
                    "family_contract_json, contract_digest, content_digest, program_entry, "
                    "runtime_profile, budget_profile, validation_summary_json"
                    ") VALUES ("
                    "'family-abi-v1', 'family-abi', 1, 'ABI family', 'ABI migration fixture', "
                    '\'{"schema": "product_family_contract_v1", '
                    '"product_structure_kind": "single_product"}\'::jsonb, '
                    "repeat('c', 64), :content_digest, 'main.py:realize', "
                    "'product_realization_v1', 'product_realization_default', '{}'::jsonb"
                    ")"
                ),
                {"content_digest": old_digest},
            )
            connection.execute(
                text(
                    "INSERT INTO product_family_geometry_bindings ("
                    "family_version_id, binding_key, geometry_resource_id, "
                    "geometry_resource_version_id, geometry_content_digest, "
                    "expected_resource_kind, geometry_purpose"
                    ") VALUES ('family-abi-v1', 'body', 'geometry-abi', "
                    "'geometry-abi-v1', repeat('a', 64), 'mapped_model', "
                    "'product_component')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO product_family_version_assets ("
                    "family_version_id, asset_role, asset_format, filename, content_type, "
                    "size_bytes, sha256"
                    ") VALUES ('family-abi-v1', 'realization_source_bundle', "
                    "'source_bundle_v1', 'source.zip', 'application/zip', 1, "
                    "repeat('e', 64))"
                )
            )

        command.upgrade(config, _AUTHORING_REVISION)
        with engine.connect() as connection:
            upgraded = connection.execute(
                text(
                    "SELECT abi_profile, content_digest FROM product_family_versions "
                    "WHERE family_version_id = 'family-abi-v1'"
                )
            ).one()
        assert tuple(upgraded) == ("product_realization_v1", new_digest)

        command.downgrade(config, _PRODUCT_FAMILY_REVISION)
        assert "abi_profile" not in _column_names(engine, "product_family_versions")
        with engine.connect() as connection:
            restored_digest = connection.execute(
                text(
                    "SELECT content_digest FROM product_family_versions "
                    "WHERE family_version_id = 'family-abi-v1'"
                )
            ).scalar_one()
        assert restored_digest == old_digest


@pytest.mark.parametrize("standard, self_geometry", [(False, False), (True, False), (True, True)])
def test_nonempty_family_downgrade_preserves_all_rows_atomically(standard, self_geometry) -> None:
    abi_column = ", abi_profile" if standard else ""
    abi_value = (", 'product_realization_v2'" if self_geometry else ", 'product_realization_v1'") if standard else ""
    usage_column = ", usage_instructions" if self_geometry else ""
    usage_value = ", :usage_instructions" if self_geometry else ""
    with _isolated_database("product_family_nonempty") as (config, engine):
        command.upgrade(config, (_HISTORICAL_HEAD_REVISION if self_geometry else "20260909_0081") if standard else _PRODUCT_FAMILY_REVISION)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO geometry_resources ("
                    "resource_id, resource_kind, owner_scope, owner_tenant_id, "
                    "owner_task_id, display_name"
                    ") VALUES ("
                    "'geometry-downgrade', 'mapped_model', 'SYSTEM', NULL, NULL, "
                    "'Downgrade geometry'"
                    ")"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO geometry_resource_versions ("
                    "resource_version_id, resource_id, version_no, origin_kind, "
                    "content_digest, manifest_digest, manifest_json, lineage_json, "
                    "validation_summary_json"
                    ") VALUES ("
                    "'geometry-downgrade-v1', 'geometry-downgrade', 1, "
                    "'trusted_seed', repeat('a', 64), repeat('b', 64), "
                    "'{}'::jsonb, '{}'::jsonb, '{\"valid\": true}'::jsonb"
                    ")"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO product_families ("
                    "family_id, owner_scope, owner_tenant_id, owner_task_id"
                    ") VALUES ("
                    "'family-downgrade', 'SYSTEM', NULL, NULL"
                    ")"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO product_family_versions ("
                    "family_version_id, family_id, version_no, display_name, purpose, "
                    "family_contract_json, contract_digest, content_digest, program_entry, "
                    "runtime_profile, budget_profile, validation_summary_json"
                    f"{abi_column}{usage_column}) VALUES ("
                    "'family-downgrade-v1', 'family-downgrade', 1, "
                    "'Downgrade family', 'Downgrade atomicity fixture', "
                    "CAST(:contract AS jsonb), "
                    "repeat('c', 64), repeat('d', 64), 'main.py:realize', "
                    "'product_realization_v1', 'product_realization_default', "
                    "'{\"valid\": true}'::jsonb"
                    f"{abi_value}{usage_value})"
                ),
                {
                    "contract": json.dumps({
                        "schema": "product_family_contract_v2" if self_geometry else "product_family_contract_v1",
                        "product_structure_kind": "single_product",
                        **({"standard_model": {"nominal_scenario_key": "nominal", "installation_frame": {},
                                               "component_keys": ["body"], "fragment_keys": ["body_shape"], "interface_keys": []}}
                           if standard else {}),
                    }),
                    "usage_instructions": _USAGE,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO product_family_geometry_bindings ("
                    "family_version_id, binding_key, geometry_resource_id, "
                    "geometry_resource_version_id, geometry_content_digest, "
                    "expected_resource_kind, geometry_purpose"
                    ") VALUES ("
                    "'family-downgrade-v1', 'body', 'geometry-downgrade', "
                    "'geometry-downgrade-v1', repeat('a', 64), "
                    "'mapped_model', 'product_component'"
                    ")"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO product_family_version_assets ("
                    "family_version_id, asset_role, asset_format, filename, content_type, "
                    "size_bytes, sha256"
                    ") VALUES ("
                    "'family-downgrade-v1', 'realization_source_bundle', "
                    "'source_bundle_v1', 'source.zip', 'application/zip', 1, "
                    "repeat('e', 64)"
                    ")"
                )
            )
            if standard:
                for role in ("validation_report", "standard_model_geometry", "standard_model_manifest"):
                    connection.execute(text(
                        "INSERT INTO product_family_version_assets "
                        "(family_version_id, asset_role, asset_format, filename, content_type, size_bytes, sha256) "
                        "VALUES ('family-downgrade-v1', :role, 'json', :filename, 'application/json', 1, repeat('f', 64))"
                    ), {"role": role, "filename": role + ".json"})

        if standard and not self_geometry:
            with engine.connect() as connection:
                before_upgrade = connection.execute(text(
                    "SELECT (to_jsonb(v) - 'usage_instructions')::text "
                    "FROM product_family_versions v"
                )).scalar_one()
            command.upgrade(config, _HISTORICAL_HEAD_REVISION)
            with engine.connect() as connection:
                assert connection.execute(text(
                    "SELECT (to_jsonb(v) - 'usage_instructions')::text "
                    "FROM product_family_versions v"
                )).scalar_one() == before_upgrade
        expected_counts = _row_counts(engine)
        assert expected_counts == {
            "product_families": 1,
            "product_family_versions": 1,
            "product_family_geometry_bindings": 1,
            "product_family_version_assets": 4 if standard else 1,
        }

        with pytest.raises(DBAPIError) as exc_info:
            command.downgrade(config, _PREVIOUS_REVISION)

        assert getattr(exc_info.value.orig, "sqlstate", None) == ("P0001" if standard and not self_geometry else "23514")
        if self_geometry:
            assert "Cannot downgrade while ProductFamily usage instructions exist" in str(exc_info.value)
        elif standard:
            assert "Cannot downgrade while immutable standard model versions exist" in str(exc_info.value)
        assert _revision(engine) == (_HISTORICAL_HEAD_REVISION if standard else _PRODUCT_FAMILY_REVISION)
        assert _table_names(engine, _PRODUCT_FAMILY_TABLES) == set(_PRODUCT_FAMILY_TABLES)
        assert _table_names(engine, _LEGACY_PRODUCT_CORE_TABLES) == set()
        assert _row_counts(engine) == expected_counts

        if standard:
            _assert_current_family_insert_guard(engine)


def _assert_current_family_insert_guard(engine: Engine) -> None:
    # Reuse a committed, complete version as a storage-shape witness. Each probe
    # rolls back, so the existing downgrade/raw-history assertions stay exact.
    modern = "product_family_contract_v2"
    abi = "product_realization_v2"
    combinations = [
        ("product_family_contract_v1", "product_realization_v1", _USAGE),
        ("product_family_contract_v1", abi, _USAGE),
        (modern, "product_realization_v1", _USAGE),
        (None, abi, _USAGE),
        (modern, None, _USAGE),
        ("unknown", abi, _USAGE),
        (modern, "unknown", _USAGE),
        ("missing", abi, _USAGE),
        (modern, abi, None),
        (modern, abi, "   \n"),
        (modern, abi, "x" * 8193),
        (modern, abi, "𐍈" * 8193),
    ]
    insert_sql = text("""
        INSERT INTO product_family_versions
        SELECT (jsonb_populate_record(NULL::product_family_versions,
            to_jsonb(v) || CAST(:overrides AS jsonb))).*
        FROM product_family_versions v WHERE family_version_id = 'family-downgrade-v1'
    """)
    with engine.connect() as connection:
        original_contract = connection.execute(text(
            "SELECT family_contract_json FROM product_family_versions "
            "WHERE family_version_id = 'family-downgrade-v1'"
        )).scalar_one()
        connection.rollback()
        for schema, profile, usage in combinations + [(modern, abi, _USAGE)]:
            transaction = connection.begin()
            try:
                contract = {k: v for k, v in original_contract.items() if k != "schema"}
                if schema != "missing":
                    contract["schema"] = schema
                overrides = json.dumps({"family_version_id": "current-probe", "version_no": 2,
                    "content_digest": "9" * 64, "abi_profile": profile,
                    "family_contract_json": contract,
                    **({} if usage == "missing" else {"usage_instructions": usage})})
                if (schema, profile, usage) != (modern, abi, _USAGE):
                    with pytest.raises(DBAPIError) as error:
                        connection.execute(insert_sql, {"overrides": overrides})
                    assert getattr(error.value.orig, "sqlstate", None) == "23514"
                    assert "productfamily" in str(error.value).replace("_", "").lower()
                else:
                    connection.execute(insert_sql, {"overrides": overrides})
                    for table in ("product_family_geometry_bindings", "product_family_version_assets"):
                        connection.execute(text(f"""
                            INSERT INTO {table}
                            SELECT (jsonb_populate_record(NULL::{table}, to_jsonb(v) ||
                                '{{"family_version_id":"current-probe"}}'::jsonb)).*
                            FROM {table} v WHERE family_version_id = 'family-downgrade-v1'
                        """))
                    connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            finally:
                transaction.rollback()


def test_0083_current_operations_preserve_legacy_raw_facts_and_state_updates() -> None:
    with _isolated_database("family_current_write") as (config, engine):
        command.upgrade(config, "20260909_0082")
        values = _seed_product_family_repair_scope(engine)
        legacy = {"family_version_id": "family-repair-v1", "family_id": "family-repair",
            "abi_profile": "product_realization_v1",
            "family_contract": {"schema": "product_family_contract_v1"}}
        with engine.begin() as connection:
            _insert_product_family_operation(connection, values, operation_id="legacy", mode="create", version_draft=legacy)
            before = connection.execute(text("SELECT operation_detail_json::text FROM critical_operations WHERE operation_id='legacy'")).scalar_one()
            artifact_before = connection.execute(text("SELECT row_to_json(v)::text FROM artifact_versions v")).all()
        command.upgrade(config, _HISTORICAL_HEAD_REVISION)
        modern = {**legacy, "abi_profile": "product_realization_v2",
            "family_contract": {"schema": "product_family_contract_v2"},
            "usage_instructions": _USAGE}
        invalid = [legacy, {**modern, "abi_profile": "product_realization_v1"},
            {**modern, "family_contract": legacy["family_contract"]},
            {**modern, "abi_profile": None}, {k: v for k, v in modern.items() if k != "abi_profile"},
            {**modern, "family_contract": {}}, {**modern, "family_contract": {"schema": None}},
            {**modern, "usage_instructions": None},
            {k: v for k, v in modern.items() if k != "usage_instructions"},
            {**modern, "usage_instructions": "   \n"},
            {**modern, "usage_instructions": "x" * 8193},
            {**modern, "usage_instructions": 7}]
        for index, draft in enumerate(invalid):
            with pytest.raises(DBAPIError) as error:
                with engine.begin() as connection:
                    _insert_product_family_operation(connection, values, operation_id=f"invalid-{index}", mode="create", version_draft=draft)
            assert getattr(error.value.orig, "sqlstate", None) == "23514"
        with engine.begin() as connection:
            _insert_product_family_operation(connection, values, operation_id="current", mode="create", version_draft=modern)
            connection.execute(text("UPDATE critical_operations SET status='failed', error_code='PRODUCT_FAMILY_BUILD_FAILED', error_message='historical failure', error_phase='execution', finished_at=CURRENT_TIMESTAMP WHERE operation_id='legacy'"))
            assert connection.execute(text("SELECT operation_detail_json::text FROM critical_operations WHERE operation_id='legacy'")).scalar_one() == before
            assert connection.execute(text("SELECT row_to_json(v)::text FROM artifact_versions v")).all() == artifact_before
        command.downgrade(config, "20260909_0082")
        with engine.begin() as connection:
            _insert_product_family_operation(connection, values, operation_id="restored-legacy", mode="create", version_draft=legacy)


def test_0082_self_geometry_operation_abi_and_atomic_downgrade() -> None:
    with _isolated_database("family_self_geometry") as (config, engine):
        command.upgrade(config, "20260909_0081")
        values = _seed_product_family_repair_scope(engine)
        legacy = {
            "family_version_id": "family-repair-v1", "family_id": "family-repair",
            "abi_profile": "product_realization_v1",
            "family_contract": {"schema": "product_family_contract_v1"},
        }
        with engine.begin() as connection:
            _insert_product_family_operation(connection, values, operation_id="legacy-v1", mode="create", version_draft=legacy)
        modern = {**legacy, "family_version_id": "family-repair-v2",
                  "abi_profile": "product_realization_v2",
                  "family_contract": {"schema": "product_family_contract_v2"},
                  "usage_instructions": _USAGE}
        with pytest.raises(DBAPIError) as error:
            with engine.begin() as connection:
                _insert_product_family_operation(connection, values, operation_id="before-v2", mode="create", version_draft=modern)
        assert getattr(error.value.orig, "sqlstate", None) == "23514"
        command.upgrade(config, _HISTORICAL_HEAD_REVISION)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT operation_detail_json -> 'version_draft' FROM critical_operations WHERE operation_id='legacy-v1'")).scalar_one() == legacy
        for index, schema in enumerate(("product_family_contract_v1", "unknown", None)):
            invalid = {**modern, "family_contract": {} if schema is None else {"schema": schema}}
            with pytest.raises(DBAPIError) as error:
                with engine.begin() as connection:
                    _insert_product_family_operation(connection, values, operation_id=f"invalid-v2-{index}", mode="create", version_draft=invalid)
            assert getattr(error.value.orig, "sqlstate", None) == "23514"
        with engine.begin() as connection:
            _insert_product_family_operation(connection, values, operation_id="modern-v2", mode="create", version_draft=modern)
        with pytest.raises(DBAPIError) as error:
            command.downgrade(config, "20260909_0081")
        assert getattr(error.value.orig, "sqlstate", None) == "23514"
        assert _revision(engine) == _HISTORICAL_HEAD_REVISION
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM critical_operations")).scalar_one() == 2


def test_0076_repair_fence_allows_only_new_attempt_identity() -> None:
    with _isolated_database("product_family_repair_fence") as (config, engine):
        command.upgrade(config, _GEOMETRY_QUERY_REVISION)
        values = _seed_product_family_repair_scope(engine)
        parent_draft = {
            "family_version_id": "family-repair-parent-version",
            "family_id": "family-repair",
            "display_name": "Repair family",
            "family_contract": {
                "schema": "product_family_contract_v1",
                "product_structure_kind": "single_product",
            },
            "abi_profile": "product_realization_v1",
            "created_at": "2026-09-05T00:00:00Z",
        }
        with engine.begin() as connection:
            _insert_product_family_operation(
                connection,
                values,
                operation_id="repair-parent",
                mode="create",
                version_draft=parent_draft,
            )
            connection.execute(
                text(
                    "UPDATE critical_operations SET status = 'failed', "
                    "error_code = 'PRODUCT_FAMILY_BUILD_FAILED', "
                    "error_message = 'fixture failure', error_phase = 'execution', "
                    "finished_at = CURRENT_TIMESTAMP "
                    "WHERE operation_id = 'repair-parent'"
                )
            )

        # Historical repair semantics are tested at their last compatible head.
        command.upgrade(config, "20260909_0082")
        child_draft = {
            **parent_draft,
            "family_version_id": "family-repair-child-version",
            "created_at": "2026-09-05T00:01:00Z",
        }
        with engine.begin() as connection:
            _insert_product_family_operation(
                connection,
                values,
                operation_id="repair-child-allowed",
                mode="repair_attempt",
                version_draft=child_draft,
                repair_parent_operation_id="repair-parent",
            )
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM critical_operations "
                        "WHERE operation_id IN ('repair-parent', 'repair-child-allowed')"
                    )
                ).scalar_one()
                == 2
            )

        semantic_changes = (
            (
                "repair-child-family-changed",
                {**child_draft, "family_id": "other-family"},
            ),
            (
                "repair-child-contract-changed",
                {
                    **child_draft,
                    "family_contract": {
                        "schema": "product_family_contract_v1",
                        "product_structure_kind": "assembly",
                    },
                },
            ),
        )
        for operation_id, changed_draft in semantic_changes:
            with pytest.raises(DBAPIError) as exc_info:
                with engine.begin() as connection:
                    _insert_product_family_operation(
                        connection,
                        values,
                        operation_id=operation_id,
                        mode="repair_attempt",
                        version_draft=changed_draft,
                        repair_parent_operation_id="repair-parent",
                    )
            assert getattr(exc_info.value.orig, "sqlstate", None) == "23503"
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM critical_operations")).scalar_one()
                == 2
            )

        command.downgrade(config, _GEOMETRY_QUERY_REVISION)
        assert _revision(engine) == _GEOMETRY_QUERY_REVISION
        with pytest.raises(DBAPIError) as exc_info:
            with engine.begin() as connection:
                _insert_product_family_operation(
                    connection,
                    values,
                    operation_id="repair-child-old-guard",
                    mode="repair_attempt",
                    version_draft={
                        **parent_draft,
                        "family_version_id": "family-repair-downgraded-version",
                        "created_at": "2026-09-05T00:02:00Z",
                    },
                    repair_parent_operation_id="repair-parent",
                )
        assert getattr(exc_info.value.orig, "sqlstate", None) == "23503"
