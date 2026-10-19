"""The feedback migration refuses legacy delivery evidence without changing it."""

from __future__ import annotations

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


@pytest.mark.parametrize("legacy_row", ["applied", "batch"])
def test_feedback_delivery_preflight_rolls_back_legacy_rows(legacy_row: str) -> None:
    source = make_url(os.environ["CAD2BIM_TEST_DATABASE_URL"])
    if source.host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("migration preflight requires local PostgreSQL")
    database_name = f"cad2bim_feedback_preflight_{legacy_row}_{uuid4().hex[:8]}"
    admin_engine = create_engine(source.set(database="postgres"))
    try:
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    finally:
        admin_engine.dispose()

    database_url = source.set(database=database_name)
    config = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.render_as_string(hide_password=False))
    command.upgrade(config, "20260913_0085")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO tenants (tenant_id, kind, status) "
                "VALUES ('tenant_preflight', 'personal', 'active')"
            ))
            connection.execute(text(
                "INSERT INTO principals (principal_id, issuer, subject, status) "
                "VALUES ('principal_preflight', 'https://preflight.example', "
                "'preflight-subject', 'active')"
            ))
            connection.execute(text(
                "INSERT INTO tenant_principals (tenant_id, principal_id, access) "
                "VALUES ('tenant_preflight', 'principal_preflight', 'owner')"
            ))
            connection.execute(text(
                "INSERT INTO users (owner_id, tenant_id) "
                "VALUES ('tenant_preflight', 'tenant_preflight')"
            ))
            connection.execute(text(
                "INSERT INTO tasks "
                "(task_id, owner_id, tenant_id, title, metadata_json, status, active_module) "
                "VALUES ('task_preflight', 'tenant_preflight', 'tenant_preflight', "
                "'Preflight', '{}'::jsonb, 'draft', 'workflow')"
            ))
            connection.execute(text(
                "INSERT INTO runs "
                "(run_id, owner_id, tenant_id, task_id, module, stage_key, "
                "input_payload, idempotency_key, request_fingerprint, thread_id, "
                "execution_mode, execution_protocol_status, status) "
                "VALUES ('run_preflight', 'tenant_preflight', 'tenant_preflight', "
                "'task_preflight', 'workflow', 'cad2bim_workflow', '{}'::jsonb, "
                "'preflight-key', repeat('a', 64), 'thread_preflight', "
                "'direct_mcp', 'not_applicable', 'completed')"
            ))
            if legacy_row == "applied":
                connection.execute(text(
                    "INSERT INTO feedback_commands "
                    "(feedback_id, accepted_sequence, owner_id, tenant_id, task_id, "
                    "run_id, author_principal_id, client_request_id, request_fingerprint, "
                    "delivery_mode, resolved_scope, body, status, claimed_batch_id, "
                    "applied_checkpoint_id, applied_at) "
                    "VALUES ('feedback_preflight', 1, 'tenant_preflight', "
                    "'tenant_preflight', 'task_preflight', 'run_preflight', "
                    "'principal_preflight', 'request_preflight', repeat('b', 64), "
                    "'steer', '{\"run_id\":\"run_preflight\",\"stage_key\":\"cad2bim_workflow\"}'::jsonb, "
                    "'Original full text', 'applied', 'old_batch', 'old_checkpoint', now())"
                ))
            else:
                connection.execute(text(
                    "INSERT INTO feedback_delivery_batches "
                    "(batch_id, owner_id, tenant_id, task_id, run_id, target_fingerprint, "
                    "claim_high_watermark, contains_interrupt, claimed_writer_generation, "
                    "claimed_checkpoint_id) VALUES "
                    "('old_batch', 'tenant_preflight', 'tenant_preflight', 'task_preflight', "
                    "'run_preflight', repeat('c', 64), 1, false, 1, NULL)"
                ))

        with pytest.raises(DBAPIError, match="lacks exact checkpoint evidence"):
            command.upgrade(config, "20260928_0092")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260913_0085"
            assert connection.scalar(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'feedback_delivery_batches' "
                "AND column_name = 'claimed_checkpoint_cursor'"
            )) == 0
            if legacy_row == "applied":
                assert connection.scalar(text(
                    "SELECT status FROM feedback_commands WHERE feedback_id = 'feedback_preflight'"
                )) == "applied"
            else:
                assert connection.scalar(text(
                    "SELECT claimed_checkpoint_id FROM feedback_delivery_batches "
                    "WHERE batch_id = 'old_batch'"
                )) is None
    finally:
        engine.dispose()
