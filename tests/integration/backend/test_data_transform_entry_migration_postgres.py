from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import DBAPIError

from CAD2BIM_backend.infrastructure.postgres.database import EXPECTED_SCHEMA_REVISION


pytestmark = [pytest.mark.integration, pytest.mark.network]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()
_REVISION = "20260904_0072"
_HEAD_REVISION = EXPECTED_SCHEMA_REVISION
_PREVIOUS_REVISION = "20260904_0071"
_DIGEST = "a" * 64


def _sync_url(value: str) -> str:
    return (
        make_url(value).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
    )


@contextmanager
def _isolated_database(prefix: str) -> Iterator[tuple[Config, Engine]]:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    schema = f"{prefix}_{uuid4().hex}"
    admin_engine = create_engine(_sync_url(TEST_DATABASE_URL), pool_pre_ping=True)
    scoped_url = make_url(_sync_url(TEST_DATABASE_URL)).update_query_dict(
        {"options": f"-csearch_path={schema}"}
    )
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        scoped_url.render_as_string(hide_password=False).replace("%", "%%"),
    )

    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped_engine = create_engine(scoped_url, pool_pre_ping=True)
        try:
            yield config, scoped_engine
        finally:
            scoped_engine.dispose()
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def _seed_scope_and_artifacts(engine: Engine) -> dict[str, str]:
    suffix = uuid4().hex[:12]
    values = {
        "principal_id": f"principal-{suffix}",
        "tenant_id": f"tenant-{suffix}",
        "owner_id": f"owner-{suffix}",
        "task_id": f"task-{suffix}",
        "run_id": f"run-{suffix}",
        "run_key": f"run-key-{suffix}",
        "thread_id": f"thread-{suffix}",
        "code_artifact_id": f"code-artifact-{suffix}",
        "code_version_id": f"code-version-{suffix}",
        "description_artifact_id": f"description-artifact-{suffix}",
        "description_version_id": f"description-version-{suffix}",
    }
    with engine.begin() as connection:
        scope_statements = (
            "INSERT INTO principals (principal_id, issuer, subject, status) "
            "VALUES (:principal_id, 'test-issuer', :owner_id, 'active')",
            "INSERT INTO tenants (tenant_id, kind, status) "
            "VALUES (:tenant_id, 'personal', 'active')",
            "INSERT INTO tenant_principals (tenant_id, principal_id, access) "
            "VALUES (:tenant_id, :principal_id, 'owner')",
            "INSERT INTO users (owner_id, tenant_id) VALUES (:owner_id, :tenant_id)",
            "INSERT INTO tasks (task_id, owner_id, tenant_id, title, metadata_json, "
            "status, active_module) VALUES (:task_id, :owner_id, :tenant_id, "
            "'Unified Data Transform migration fixture', '{}'::jsonb, "
            "'running', 'data_runtime')",
            "INSERT INTO runs (run_id, owner_id, tenant_id, task_id, module, stage_key, "
            "input_payload, idempotency_key, request_fingerprint, thread_id, status, "
            "execution_protocol_status, execution_mode) VALUES (:run_id, :owner_id, "
            ":tenant_id, :task_id, 'data_runtime', 'direct', '{}'::jsonb, :run_key, "
            "repeat('b', 64), :thread_id, 'running', 'not_applicable', 'direct_mcp')",
        )
        for statement in scope_statements:
            connection.execute(text(statement), values)
        for role in ("code", "description"):
            artifact_values = {
                **values,
                "artifact_id": values[f"{role}_artifact_id"],
                "version_id": values[f"{role}_version_id"],
                "kind": f"data_transform_{role}",
                "filename": f"transform.{'sql' if role == 'code' else 'md'}",
                "content_type": "application/sql" if role == "code" else "text/markdown",
                "idempotency_key": f"{role}-artifact-key-{suffix}",
                "request_fingerprint": hashlib.sha256(role.encode()).hexdigest(),
                "storage_key": f"migration/{suffix}/{role}",
                "sha256": _DIGEST,
            }
            connection.execute(
                text(
                    """
                    INSERT INTO artifacts (
                        artifact_id, owner_id, tenant_id, task_id, run_id,
                        kind, filename, content_type, status,
                        idempotency_key, request_fingerprint
                    ) VALUES (
                        :artifact_id, :owner_id, :tenant_id, :task_id, :run_id,
                        :kind, :filename, :content_type, 'available',
                        :idempotency_key, :request_fingerprint
                    )
                    """
                ),
                artifact_values,
            )
            connection.execute(
                text(
                    """
                    INSERT INTO artifact_versions (
                        version_id, owner_id, tenant_id, task_id, artifact_id,
                        version_no, storage_key, filename, content_type,
                        size_bytes, sha256, metadata_json, lineage_json
                    ) VALUES (
                        :version_id, :owner_id, :tenant_id, :task_id,
                        :artifact_id, 1, :storage_key, :filename, :content_type,
                        1, :sha256, '{}'::jsonb, '{}'::jsonb
                    )
                    """
                ),
                artifact_values,
            )
    return values


def _insert_operation(
    connection: Connection,
    values: dict[str, str],
    *,
    operation_id: str,
    tool_name: str,
    repair_parent_operation_id: str | None = None,
    with_description: bool = False,
    with_partial_description: bool = False,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO critical_operations (
                operation_id, owner_id, tenant_id, task_id, run_id,
                operation_kind, initiator, tool_name, intent_summary,
                source_filename, idempotency_key, request_fingerprint,
                status, result_package_id, error_code, error_message,
                error_phase, code_artifact_id, code_artifact_version_id,
                code_artifact_sha256, description_artifact_id,
                description_artifact_version_id, description_artifact_sha256,
                repair_parent_operation_id, base_ifc_revision_id,
                expected_ifc_write_epoch, target_geometry_resource_id,
                result_geometry_resource_version_id, operation_detail_json,
                started_at, finished_at
            ) VALUES (
                :operation_id, :owner_id, :tenant_id, :task_id, :run_id,
                'data_transform', 'direct_client', :tool_name,
                :intent_summary, NULL, :idempotency_key, :request_fingerprint,
                'prepared', NULL, NULL, NULL, NULL,
                :code_artifact_id, :code_version_id, :code_sha256,
                :description_artifact_id, :description_version_id,
                :description_sha256, :repair_parent_operation_id,
                NULL, NULL, NULL, NULL, '{}'::jsonb, CURRENT_TIMESTAMP, NULL
            )
            """
        ),
        {
            **values,
            "operation_id": operation_id,
            "tool_name": tool_name,
            "intent_summary": f"Migration fixture for {tool_name}",
            "idempotency_key": f"idempotency-{operation_id}",
            "request_fingerprint": hashlib.sha256(operation_id.encode()).hexdigest(),
            "code_sha256": _DIGEST,
            "description_artifact_id": (
                values["description_artifact_id"]
                if with_description or with_partial_description
                else None
            ),
            "description_version_id": (
                values["description_version_id"] if with_description else None
            ),
            "description_sha256": _DIGEST if with_description else None,
            "repair_parent_operation_id": repair_parent_operation_id,
        },
    )


def _seed_legacy_operations(engine: Engine, values: dict[str, str]) -> None:
    with engine.begin() as connection:
        _insert_operation(
            connection,
            values,
            operation_id="legacy-create",
            tool_name="data_transform",
            with_description=True,
        )
        _insert_operation(
            connection,
            values,
            operation_id="legacy-repair",
            tool_name="data_repair_transform",
            repair_parent_operation_id="legacy-create",
            with_description=True,
        )
        _insert_operation(
            connection,
            values,
            operation_id="legacy-cad-measurement",
            tool_name="cad_compute_measurements",
            with_description=True,
        )


def _operation_snapshot(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    """
                    SELECT operation_id, operation_kind, tool_name,
                           intent_summary, code_artifact_id,
                           code_artifact_version_id, code_artifact_sha256,
                           description_artifact_id,
                           description_artifact_version_id,
                           description_artifact_sha256,
                           repair_parent_operation_id, operation_detail_json
                    FROM critical_operations
                    ORDER BY operation_id
                    """
                )
            ).mappings()
        ]


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_0111_preserves_original_producers_and_enforces_exact_ifc_source_pair():
    """Migration row fixtures prove constraints, not an executed extraction or RLS grant."""
    with _isolated_database("derive_read_source") as (config, engine):
        command.upgrade(config, "20261014_0110")
        values = _seed_scope_and_artifacts(engine)
        _seed_legacy_operations(engine, values)
        snapshot_sql = text("SELECT operation_id, to_jsonb(operation) FROM critical_operations operation ORDER BY operation_id")
        with engine.connect() as connection:
            before = dict(connection.execute(snapshot_sql).all())
        command.upgrade(config, "20261017_0111")
        with engine.connect() as connection:
            after = dict(connection.execute(snapshot_sql).all())
            assert set(after) == set(before)
            for key, record in before.items():
                assert {name: after[key][name] for name in record} == record
                assert after[key]["source_ifc_model_id"] is None
                assert after[key]["source_ifc_revision_id"] is None
            constraint = connection.execute(text(
                "SELECT convalidated, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid='critical_operations'::regclass "
                "AND conname='fk_critical_operations_ifc_read_source'"
            )).one()
            assert constraint.convalidated
            assert "(owner_id, task_id, run_id, source_ifc_model_id, source_ifc_revision_id)" in constraint[1]
            assert "REFERENCES ifc_model_revisions(owner_id, task_id, run_id, ifc_model_id, revision_id)" in constraint[1]
        with engine.begin() as connection:
            _insert_operation(connection, values, operation_id="current-derive", tool_name="data_derive",
                with_description=True)
        for source_sql, expected_state in (
            ("source_ifc_model_id='missing-model'", "23514"),
            ("source_ifc_model_id='missing-model', source_ifc_revision_id='missing-revision', "
             "operation_detail_json='{\"detail_kind\":\"data_transform\",\"engine\":\"python_arrow\"}'::jsonb", "23503"),
        ):
            with pytest.raises(DBAPIError) as rejected:
                with engine.begin() as connection:
                    connection.execute(text(f"UPDATE critical_operations SET {source_sql} WHERE operation_id='current-derive'"))
            assert rejected.value.orig.sqlstate == expected_state
        with engine.connect() as connection:
            before_downgrade = dict(connection.execute(snapshot_sql).all())
        with pytest.raises(DBAPIError, match="Cannot discard Data derivation Operations"):
            command.downgrade(config, "20261014_0110")
        assert _revision(engine) == "20261017_0111"
        with engine.connect() as connection:
            assert dict(connection.execute(snapshot_sql).all()) == before_downgrade


def test_fresh_head_is_unified_data_transform_entry_revision() -> None:
    with _isolated_database("data_transform_entry_head") as (config, engine):
        command.upgrade(config, "head")

        assert _revision(engine) == _HEAD_REVISION


def test_0072_preserves_legacy_rows_and_enforces_closed_operation_shapes() -> None:
    with _isolated_database("data_transform_entry_upgrade") as (config, engine):
        command.upgrade(config, _PREVIOUS_REVISION)
        values = _seed_scope_and_artifacts(engine)
        _seed_legacy_operations(engine, values)
        legacy_snapshot = _operation_snapshot(engine)

        command.upgrade(config, _REVISION)

        assert _revision(engine) == _REVISION
        assert _operation_snapshot(engine) == legacy_snapshot
        with engine.begin() as connection:
            _insert_operation(
                connection,
                values,
                operation_id="unified-repair",
                tool_name="data_transform",
                repair_parent_operation_id="legacy-create",
                with_description=True,
            )
            _insert_operation(
                connection,
                values,
                operation_id="unified-create-with-description",
                tool_name="data_transform",
                with_description=True,
            )

        invalid_shapes = (
            {
                "operation_id": "unified-repair-without-description",
                "tool_name": "data_transform",
                "repair_parent_operation_id": "legacy-create",
                "with_description": False,
            },
            {
                "operation_id": "unified-create-with-partial-description",
                "tool_name": "data_transform",
                "repair_parent_operation_id": None,
                "with_partial_description": True,
            },
            {
                "operation_id": "legacy-repair-without-parent",
                "tool_name": "data_repair_transform",
                "repair_parent_operation_id": None,
                "with_description": True,
            },
            {
                "operation_id": "cad-measurement-with-repair-fields",
                "tool_name": "cad_compute_measurements",
                "repair_parent_operation_id": "legacy-create",
                "with_description": True,
            },
        )
        for invalid in invalid_shapes:
            try:
                with engine.begin() as connection:
                    _insert_operation(connection, values, **invalid)
            except DBAPIError as error:
                assert error.orig.sqlstate == "23514"
            else:
                pytest.fail(f"invalid Operation shape was accepted: {invalid['operation_id']}")


def test_0072_legacy_only_downgrade_preserves_rows_and_reupgrades() -> None:
    with _isolated_database("data_transform_entry_legacy_roundtrip") as (config, engine):
        command.upgrade(config, _PREVIOUS_REVISION)
        values = _seed_scope_and_artifacts(engine)
        _seed_legacy_operations(engine, values)
        legacy_snapshot = _operation_snapshot(engine)
        command.upgrade(config, _REVISION)

        command.downgrade(config, _PREVIOUS_REVISION)

        assert _revision(engine) == _PREVIOUS_REVISION
        assert _operation_snapshot(engine) == legacy_snapshot
        command.upgrade(config, _REVISION)
        assert _revision(engine) == _REVISION
        assert _operation_snapshot(engine) == legacy_snapshot


def test_0072_unified_repair_blocks_downgrade_atomically_then_roundtrips() -> None:
    with _isolated_database("data_transform_entry_fail_closed") as (config, engine):
        command.upgrade(config, _PREVIOUS_REVISION)
        values = _seed_scope_and_artifacts(engine)
        _seed_legacy_operations(engine, values)
        command.upgrade(config, _REVISION)
        with engine.begin() as connection:
            _insert_operation(
                connection,
                values,
                operation_id="unified-repair",
                tool_name="data_transform",
                repair_parent_operation_id="legacy-create",
                with_description=True,
            )
        before_failed_downgrade = _operation_snapshot(engine)

        with pytest.raises(DBAPIError) as exc_info:
            command.downgrade(config, _PREVIOUS_REVISION)

        assert exc_info.value.orig.sqlstate == "23514"
        assert _revision(engine) == _REVISION
        assert _operation_snapshot(engine) == before_failed_downgrade

        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM critical_operations WHERE operation_id = 'unified-repair'")
            )
        legacy_snapshot = _operation_snapshot(engine)
        command.downgrade(config, _PREVIOUS_REVISION)
        assert _revision(engine) == _PREVIOUS_REVISION
        assert _operation_snapshot(engine) == legacy_snapshot
        command.upgrade(config, _REVISION)
        assert _revision(engine) == _REVISION
        assert _operation_snapshot(engine) == legacy_snapshot
