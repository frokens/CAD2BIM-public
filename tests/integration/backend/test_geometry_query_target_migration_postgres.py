from __future__ import annotations

import json
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
_AUTHORING_REVISION = "20260904_0074"
_HEAD_REVISION = EXPECTED_SCHEMA_REVISION
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


def _insert_artifact(
    connection: Connection,
    *,
    artifact_id: str,
    version_id: str,
    kind: str,
    content_type: str,
    metadata_json: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO artifacts (
                artifact_id, owner_id, tenant_id, task_id, run_id, kind, filename,
                content_type, status, idempotency_key, request_fingerprint
            ) VALUES (
                :artifact_id, 'owner-query-target', 'tenant-query-target',
                'task-query-target', NULL, :kind, :filename, :content_type,
                'available', :idempotency_key, :request_fingerprint
            )
            """
        ),
        {
            "artifact_id": artifact_id,
            "kind": kind,
            "filename": f"{artifact_id}.bin",
            "content_type": content_type,
            "idempotency_key": f"idempotency-{artifact_id}",
            "request_fingerprint": _DIGEST,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO artifact_versions (
                version_id, owner_id, tenant_id, task_id, artifact_id, version_no,
                storage_key, filename, content_type, size_bytes, sha256,
                metadata_json, lineage_json
            ) VALUES (
                :version_id, 'owner-query-target', 'tenant-query-target',
                'task-query-target', :artifact_id, 1, :storage_key, :filename,
                :content_type, 1, :sha256, CAST(:metadata_json AS jsonb), '{}'::jsonb
            )
            """
        ),
        {
            "artifact_id": artifact_id,
            "version_id": version_id,
            "storage_key": f"migration/{artifact_id}",
            "filename": f"{artifact_id}.bin",
            "content_type": content_type,
            "sha256": _DIGEST,
            "metadata_json": metadata_json,
        },
    )


def _seed_0074_query_target_fixture(engine: Engine) -> None:
    with engine.begin() as connection:
        for statement in (
            """
                INSERT INTO principals (principal_id, issuer, subject, status)
                VALUES ('principal-query-target', 'migration-test',
                        'owner-query-target', 'active')
            """,
            """
                INSERT INTO tenants (tenant_id, kind, status)
                VALUES ('tenant-query-target', 'personal', 'active')
            """,
            """
                INSERT INTO tenant_principals (tenant_id, principal_id, access)
                VALUES ('tenant-query-target', 'principal-query-target', 'owner')
            """,
            """
                INSERT INTO users (owner_id, tenant_id)
                VALUES ('owner-query-target', 'tenant-query-target')
            """,
            """
                INSERT INTO tasks (
                    task_id, owner_id, tenant_id, title, metadata_json, status, active_module
                ) VALUES (
                    'task-query-target', 'owner-query-target', 'tenant-query-target',
                    'Geometry query target migration', '{}'::jsonb, 'running', 'ifc'
                )
            """,
            """
                INSERT INTO geometry_resources (
                    resource_id, resource_kind, owner_scope, owner_tenant_id,
                    owner_task_id, display_name
                ) VALUES (
                    'geometry-query-target', 'mapped_model', 'SYSTEM', NULL, NULL,
                    'Geometry query target migration fixture'
                )
            """,
            """
                INSERT INTO geometry_resource_versions (
                    resource_version_id, resource_id, version_no, origin_kind,
                    content_digest, manifest_digest, manifest_json, lineage_json,
                    validation_summary_json
                ) VALUES (
                    'geometry-query-target-v1', 'geometry-query-target', 1,
                    'migration_fixture', repeat('b', 64), repeat('c', 64),
                    '{}'::jsonb, '{}'::jsonb, '{"valid": true}'::jsonb
                )
            """,
        ):
            connection.execute(text(statement))
        _insert_artifact(
            connection,
            artifact_id="query-source",
            version_id="query-source-v1",
            kind="geometry_query_source",
            content_type="text/x-python",
            metadata_json="{}",
        )
        _insert_artifact(
            connection,
            artifact_id="preview-target",
            version_id="preview-target-v1",
            kind="product_realization_preview_geometry",
            content_type="application/vnd.opencascade.brep",
            metadata_json=(
                '{"target_kind":"product_realization_preview",'
                '"geometry_format":"occt_brep_v1",'
                '"geometry_sha256":"' + _DIGEST + '",'
                '"family_version_id":"family-version-v1",'
                '"family_version_content_digest":"' + "d" * 64 + '",'
                '"solve_digest":"' + "e" * 64 + '"}'
            ),
        )
        _insert_artifact(
            connection,
            artifact_id="invalid-preview-target",
            version_id="invalid-preview-target-v1",
            kind="product_realization_preview_geometry",
            content_type="application/vnd.opencascade.brep",
            metadata_json=(
                '{"target_kind":"product_realization_preview",'
                '"geometry_format":"occt_brep_v1",'
                '"geometry_sha256":"' + _DIGEST + '",'
                '"family_version_id":"family-version-v1",'
                '"family_version_content_digest":"not-a-digest",'
                '"solve_digest":"' + "e" * 64 + '"}'
            ),
        )
        connection.execute(
            text(
                """
                INSERT INTO geometry_query_attempts (
                    attempt_id, tenant_id, task_id, origin_run_id,
                    resource_version_id, source_artifact_version_id,
                    result_artifact_version_id, repair_parent_attempt_id,
                    request_identity, request_digest, status, diagnostics_json,
                    finished_at
                ) VALUES (
                    'legacy-resource-attempt', 'tenant-query-target',
                    'task-query-target', NULL, 'geometry-query-target-v1',
                    'query-source-v1', NULL, NULL, 'legacy-resource-request',
                    repeat('f', 64), 'PREPARED', '[]'::jsonb, NULL
                )
                """
            )
        )


def _insert_preview_attempt(
    engine: Engine,
    *,
    attempt_id: str,
    target_version_id: str,
    resource_version_id: str | None = None,
    target_kind: str = "product_realization_preview",
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO geometry_query_attempts (
                    attempt_id, tenant_id, task_id, origin_run_id, target_kind,
                    resource_version_id, target_artifact_version_id,
                    target_geometry_format, source_artifact_version_id,
                    result_artifact_version_id, repair_parent_attempt_id,
                    request_identity, request_digest, status, diagnostics_json,
                    finished_at
                ) VALUES (
                    :attempt_id, 'tenant-query-target', 'task-query-target', NULL,
                    :target_kind, :resource_version_id,
                    :target_version_id, 'occt_brep_v1', 'query-source-v1', NULL,
                    NULL, :attempt_id, repeat('1', 64), 'PREPARED', '[]'::jsonb, NULL
                )
                """
            ),
            {
                "attempt_id": attempt_id,
                "resource_version_id": resource_version_id,
                "target_version_id": target_version_id,
                "target_kind": target_kind,
            },
        )


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_0082_geometry_preview_target_is_exact_and_preserves_family_preview() -> None:
    with _isolated_database("geometry_preview_target") as (config, engine):
        command.upgrade(config, _AUTHORING_REVISION)
        _seed_0074_query_target_fixture(engine)
        command.upgrade(config, "20260909_0081")
        metadata = {
            "target_kind": "geometry_resource_preview", "geometry_format": "occt_brep_v1",
            "geometry_sha256": _DIGEST, "resource_id": "geometry-query-target",
            "resource_version_id": "geometry-query-target-v1",
            "resource_content_digest": "b" * 64, "derived_only": True,
            "sample_parameters_sha256": "",
        }
        with engine.begin() as connection:
            _insert_artifact(connection, artifact_id="geometry-preview", version_id="geometry-preview-v1",
                             kind="geometry_preview_sample_geometry", content_type="application/vnd.opencascade.brep",
                             metadata_json=json.dumps(metadata))
        with pytest.raises(DBAPIError):
            _insert_preview_attempt(engine, attempt_id="not-yet-supported", target_version_id="geometry-preview-v1", target_kind="geometry_preview")
        command.upgrade(config, "20260909_0082")
        _insert_preview_attempt(engine, attempt_id="family-retained", target_version_id="preview-target-v1")
        _insert_preview_attempt(engine, attempt_id="geometry-accepted", target_version_id="geometry-preview-v1", target_kind="geometry_preview")
        for index, patch in enumerate((
            {"target_kind": "product_realization_preview"}, {"geometry_sha256": "0" * 64},
            {"resource_id": ""}, {"resource_version_id": ""},
            {"resource_content_digest": "invalid"}, {"derived_only": False},
        )):
            with engine.begin() as connection:
                _insert_artifact(connection, artifact_id=f"invalid-geometry-{index}", version_id=f"invalid-geometry-{index}-v1",
                                 kind="geometry_preview_sample_geometry", content_type="application/vnd.opencascade.brep",
                                 metadata_json=json.dumps({**metadata, **patch}))
            with pytest.raises(DBAPIError) as error:
                _insert_preview_attempt(engine, attempt_id=f"invalid-attempt-{index}", target_version_id=f"invalid-geometry-{index}-v1", target_kind="geometry_preview")
            assert getattr(error.value.orig, "sqlstate", None) == "23503"
        for attempt_id, target, kind in (("cross-kind-family", "geometry-preview-v1", "product_realization_preview"),
                                         ("cross-kind-geometry", "preview-target-v1", "geometry_preview")):
            with pytest.raises(DBAPIError) as error:
                _insert_preview_attempt(engine, attempt_id=attempt_id, target_version_id=target, target_kind=kind)
            assert getattr(error.value.orig, "sqlstate", None) == "23503"
        with pytest.raises(DBAPIError) as error:
            command.downgrade(config, "20260909_0081")
        assert getattr(error.value.orig, "sqlstate", None) == "23514"
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260909_0082"
            assert connection.execute(text("SELECT count(*) FROM geometry_query_attempts")).scalar_one() == 3


def test_0075_backfills_legacy_rows_and_resource_only_downgrade_is_lossless() -> None:
    with _isolated_database("geometry_query_target_backfill") as (config, engine):
        command.upgrade(config, _AUTHORING_REVISION)
        _seed_0074_query_target_fixture(engine)
        facts_queries = {
            "attempts": text("SELECT attempt_id, to_jsonb(attempt) FROM geometry_query_attempts attempt"),
            "artifacts": text("SELECT version_id, to_jsonb(version) FROM artifact_versions version"),
        }
        with engine.connect() as connection:
            before_0075 = {name: dict(connection.execute(query).all())
                for name, query in facts_queries.items()}

        # 0075 owns this backfill and reversible resource-only boundary. Current
        # head deliberately cannot downgrade through later contract cutovers.
        command.upgrade(config, "20260905_0075")

        assert _revision(engine) == "20260905_0075"
        with engine.connect() as connection:
            legacy = connection.execute(
                text(
                    "SELECT target_kind, resource_version_id, target_artifact_version_id, "
                    "target_geometry_format FROM geometry_query_attempts "
                    "WHERE attempt_id = 'legacy-resource-attempt'"
                )
            ).one()
            assert tuple(legacy) == (
                "geometry_resource_version",
                "geometry-query-target-v1",
                None,
                None,
            )
            columns = {
                row.column_name: (row.is_nullable, row.column_default)
                for row in connection.execute(
                    text(
                        "SELECT column_name, is_nullable, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'geometry_query_attempts'"
                    )
                )
            }
            assert columns["target_kind"] == ("NO", None)
            assert columns["resource_version_id"][0] == "YES"
            security = connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = 'geometry_query_attempts'::regclass"
                )
            ).one()
            assert tuple(security) == (True, True)
            policy_commands = set(
                connection.execute(
                    text(
                        "SELECT cmd FROM pg_policies WHERE schemaname = current_schema() "
                        "AND tablename = 'geometry_query_attempts'"
                    )
                ).scalars()
            )
            assert policy_commands == {"SELECT", "INSERT", "UPDATE"}
            indexes = set(
                connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
                        "AND tablename = 'geometry_query_attempts'"
                    )
                ).scalars()
            )
            assert "ix_geometry_query_attempts_target_artifact_created" in indexes
            delete_action = connection.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint "
                    "WHERE conname = 'fk_geometry_query_attempts_target_artifact_version' "
                    "AND conrelid = 'geometry_query_attempts'::regclass"
                )
            ).scalar_one()
            assert delete_action == "r"

        command.downgrade(config, _AUTHORING_REVISION)

        assert _revision(engine) == _AUTHORING_REVISION
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT resource_version_id FROM geometry_query_attempts "
                        "WHERE attempt_id = 'legacy-resource-attempt'"
                    )
                ).scalar_one()
                == "geometry-query-target-v1"
            )
            columns = set(
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'geometry_query_attempts'"
                    )
                ).scalars()
            )
            assert (
                not {
                    "target_kind",
                    "target_artifact_version_id",
                    "target_geometry_format",
                }
                & columns
            )
            assert {name: dict(connection.execute(query).all())
                for name, query in facts_queries.items()} == before_0075

        command.upgrade(config, "20260905_0075")
        with engine.connect() as connection:
            before_head = {name: dict(connection.execute(query).all())
                for name, query in facts_queries.items()}
        command.upgrade(config, _HEAD_REVISION)
        assert _revision(engine) == _HEAD_REVISION
        with engine.connect() as connection:
            after_head = {name: dict(connection.execute(query).all())
                for name, query in facts_queries.items()}
        for name, records in before_head.items():
            assert set(after_head[name]) == set(records)
            for identity, original in records.items():
                for field, value in original.items():
                    assert after_head[name][identity][field] == value, (name, identity, field)


def test_0075_enforces_xor_exact_preview_metadata_fk_and_fail_closed_downgrade() -> None:
    with _isolated_database("geometry_query_target_closed") as (config, engine):
        command.upgrade(config, _AUTHORING_REVISION)
        _seed_0074_query_target_fixture(engine)
        command.upgrade(config, "20260905_0075")

        with pytest.raises(DBAPIError) as xor_error:
            _insert_preview_attempt(
                engine,
                attempt_id="invalid-xor-attempt",
                target_version_id="preview-target-v1",
                resource_version_id="geometry-query-target-v1",
            )
        assert getattr(xor_error.value.orig, "sqlstate", None) == "23514"

        with pytest.raises(DBAPIError) as metadata_error:
            _insert_preview_attempt(
                engine,
                attempt_id="invalid-metadata-attempt",
                target_version_id="invalid-preview-target-v1",
            )
        assert getattr(metadata_error.value.orig, "sqlstate", None) == "23503"

        _insert_preview_attempt(
            engine,
            attempt_id="valid-preview-attempt",
            target_version_id="preview-target-v1",
        )

        with pytest.raises(DBAPIError) as fk_error:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM artifact_versions WHERE version_id = 'preview-target-v1'")
                )
        assert getattr(fk_error.value.orig, "sqlstate", None) == "23503"

        facts_queries = {
            "attempts": text("SELECT attempt_id, to_jsonb(attempt) FROM geometry_query_attempts attempt"),
            "artifacts": text("SELECT version_id, to_jsonb(version) FROM artifact_versions version"),
        }
        with engine.connect() as connection:
            before_downgrade = {name: dict(connection.execute(query).all())
                for name, query in facts_queries.items()}
        with pytest.raises(DBAPIError) as downgrade_error:
            command.downgrade(config, _AUTHORING_REVISION)
        assert getattr(downgrade_error.value.orig, "sqlstate", None) == "23514"
        assert _revision(engine) == "20260905_0075"
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM geometry_query_attempts "
                        "WHERE attempt_id = 'valid-preview-attempt'"
                    )
                ).scalar_one()
                == 1
            )
            assert {name: dict(connection.execute(query).all())
                for name, query in facts_queries.items()} == before_downgrade

        command.upgrade(config, _HEAD_REVISION)
        assert _revision(engine) == _HEAD_REVISION
        with engine.connect() as connection:
            after_head = {name: dict(connection.execute(query).all())
                for name, query in facts_queries.items()}
        for name, records in before_downgrade.items():
            assert set(after_head[name]) == set(records)
            for identity, original in records.items():
                # Preserve all pre-existing target identity, scope, state and
                # exact Artifact metadata; additional head fields are separate.
                for field, value in original.items():
                    assert after_head[name][identity][field] == value, (name, identity, field)
