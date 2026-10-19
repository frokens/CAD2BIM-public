from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


pytestmark = [pytest.mark.integration, pytest.mark.network]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL")

_REMOVED_PROTOCOL_DIGEST = "ffe895ec8299813b2a901802b04723f78338053969e0aacd6eb5f6f04e1ab830"
_REMOVED_TOPOLOGY_FINGERPRINT = (
    "243e0fba849acd4a88efeb5cb833dc55f1cdefce7d7faed33fd7595c2de1cc1a"
)
_REMOVED_PROTOCOL_BINDING = {
    "schema_version": "cad2bim-execution-protocol-binding",
    "bundle_id": "cad2bim.workflow.bundle",
    "module": "workflow",
    "stage_key": "cad2bim_workflow",
    "protocol": {
        "schema_version": "cad2bim-execution-protocol",
        "protocol_id": "cad2bim-workflow",
        "workflow_graph_version": "cad2bim.workflow",
        "requirement_graph_version": "cad2bim.requirement",
        "cad_graph_version": "cad2bim.cad-extraction",
        "ifc_generation_graph_version": "cad2bim.ifc-generation",
        "ifc_stage_graph_version": "cad2bim.ifc-stage",
        "state_schema_version": "cad2bim.workflow-state",
        "plan_compiler_version": "cad2bim-global-ifc-plan",
        "component_protocol_version": "declarative-component-type",
        "skill_policy_version": "context-session-current",
        "tool_catalog_digest": (
            "72008afd1b63fe7452f5f8c969f4ee4c2c1407018e933f351e99fdf9a31b4cba"
        ),
        "topology_fingerprint": _REMOVED_TOPOLOGY_FINGERPRINT,
    },
    "protocol_digest": _REMOVED_PROTOCOL_DIGEST,
}


def _sync_database_url(value: str) -> str:
    return (
        make_url(value)
        .set(drivername="postgresql+psycopg")
        .render_as_string(hide_password=False)
    )


def _cursor(checkpoint_id: str) -> dict[str, object]:
    return {
        "schema_version": "1",
        "thread_id": "thread-old-contract",
        "checkpoint_id": checkpoint_id,
        "checkpoint_ns": "",
        "root_checkpoint_id": checkpoint_id,
        "checkpoint_map": [["", checkpoint_id]],
        "checkpoint_digest": "d" * 64,
    }


def test_checkpoint_topology_cleanup_preserves_exact_protocol_and_saved_link() -> None:
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import Session
    from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocol, ExecutionProtocolBinding
    from CAD2BIM_backend.infrastructure.postgres.models import CheckpointLinkModel
    from CAD2BIM_backend.infrastructure.postgres.repository_artifact import PostgresArtifactRepositoryMixin
    from tests.integration.backend.test_geometry_resource_migrations import _isolated_database

    binding = ExecutionProtocolBinding.from_protocol(
        ExecutionProtocol(protocol_id="checkpoint-cleanup"), bundle_id="checkpoint-cleanup",
        module="workflow", stage_key="cad2bim_workflow",
    )
    with _isolated_database("checkpoint_topology") as (config, engine):
        command.upgrade(config, "20261006_0106")
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO tenants (tenant_id, kind, status) VALUES ('cleanup-tenant','personal','active');
                INSERT INTO users (owner_id, tenant_id) VALUES ('cleanup-owner','cleanup-tenant');
                INSERT INTO tasks (task_id,owner_id,tenant_id,title,metadata_json,status,active_module)
                VALUES ('cleanup-task','cleanup-owner','cleanup-tenant','Checkpoint cleanup','{}','running','workflow');
            """))
            connection.execute(text("""
                INSERT INTO runs (run_id,owner_id,tenant_id,task_id,module,stage_key,input_payload,
                    idempotency_key,request_fingerprint,thread_id,execution_protocol_status,
                    execution_protocol,execution_protocol_id,execution_protocol_digest,status)
                VALUES ('cleanup-run','cleanup-owner','cleanup-tenant','cleanup-task','workflow',
                    'cad2bim_workflow','{}','cleanup-run',repeat('a',64),'cleanup-thread','pinned',
                    CAST(:binding AS jsonb),:protocol_id,:digest,'waiting_user')
            """), {"binding": binding.model_dump_json(), "protocol_id": binding.protocol.protocol_id,
                     "digest": binding.protocol_digest})
            connection.execute(text("""
                INSERT INTO checkpoint_links (savepoint_id,owner_id,tenant_id,task_id,run_id,thread_id,
                    safe_point_key,checkpoint_id,checkpoint_ns,execution_protocol_id,execution_protocol_digest,
                    topology_fingerprint,stage_key,checkpoint_next,safe_point_context,default_route,
                    writer_generation,artifact_ids,idempotency_key,request_fingerprint)
                VALUES ('cleanup-savepoint','cleanup-owner','cleanup-tenant','cleanup-task','cleanup-run',
                    'cleanup-thread','accepted','cleanup-checkpoint','',:protocol_id,:digest,repeat('b',64),
                    'cad2bim_workflow','[]','accepted','complete',0,'[]','cleanup-savepoint',repeat('c',64))
            """), {"protocol_id": binding.protocol.protocol_id, "digest": binding.protocol_digest})
        command.upgrade(config, "head")
        with Session(engine) as session:
            saved = session.get(CheckpointLinkModel, "cleanup-savepoint")
            record = PostgresArtifactRepositoryMixin._savepoint_record(saved)
            assert (record.run_id, record.checkpoint_id, record.execution_protocol_id,
                    record.execution_protocol_digest) == (
                "cleanup-run", "cleanup-checkpoint", binding.protocol.protocol_id, binding.protocol_digest,
            )
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name='checkpoint_links' AND column_name='topology_fingerprint'"
            )).scalar_one() == 0
        with pytest.raises(DBAPIError, match="protocol fields are immutable"):
            with engine.begin() as connection:
                connection.execute(text("UPDATE checkpoint_links SET execution_protocol_digest=repeat('d',64) "
                                        "WHERE savepoint_id='cleanup-savepoint'"))


def test_checkpoint_graph_contract_hard_cut_quarantines_old_resumable_runs() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    schema = f"checkpoint_graph_contract_{uuid4().hex}"
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
        command.upgrade(config, "20260809_0042")
        scoped_engine = create_engine(migration_url, pool_pre_ping=True)
        try:
            with scoped_engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO tenants (tenant_id, kind, status)
                        VALUES ('tenant-old-contract', 'personal', 'active');
                        INSERT INTO users (owner_id, tenant_id)
                        VALUES ('owner-old-contract', 'tenant-old-contract');
                        INSERT INTO tasks (
                            task_id, owner_id, tenant_id, title, metadata_json,
                            status, active_module
                        ) VALUES (
                            'task-old-contract', 'owner-old-contract',
                            'tenant-old-contract', 'Old contract', '{}'::jsonb,
                            'running', 'workflow'
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO runs (
                            run_id, owner_id, tenant_id, task_id, module, stage_key,
                            input_payload, idempotency_key, request_fingerprint,
                            thread_id, execution_protocol_status, execution_protocol,
                            execution_protocol_id, execution_protocol_digest, status
                        ) VALUES (
                            'run-old-contract', 'owner-old-contract',
                            'tenant-old-contract', 'task-old-contract', 'workflow',
                            'cad2bim_workflow', '{}'::jsonb, 'run-old-contract',
                            repeat('a', 64), 'thread-old-contract', 'pinned',
                            CAST(:binding AS jsonb), 'cad2bim-workflow',
                            :protocol_digest, 'waiting_user'
                        )
                        """
                    ),
                    {
                        "binding": json.dumps(_REMOVED_PROTOCOL_BINDING),
                        "protocol_digest": _REMOVED_PROTOCOL_DIGEST,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO checkpoint_links (
                            savepoint_id, owner_id, tenant_id, task_id, run_id,
                            thread_id, safe_point_key, checkpoint_id, checkpoint_ns,
                            execution_protocol_id, execution_protocol_digest,
                            topology_fingerprint, stage_key, checkpoint_next,
                            safe_point_context, default_route, writer_generation,
                            artifact_ids, idempotency_key, request_fingerprint
                        ) VALUES (
                            'savepoint-old-contract', 'owner-old-contract',
                            'tenant-old-contract', 'task-old-contract',
                            'run-old-contract', 'thread-old-contract', 'stage_accepted',
                            'checkpoint-old-contract', '', 'cad2bim-workflow',
                            :protocol_digest, :topology_fingerprint, 'stage-old',
                            '[]'::jsonb, 'stage accepted', 'complete', 1, '[]'::jsonb,
                            'savepoint-old-contract', repeat('b', 64)
                        )
                        """
                    ),
                    {
                        "protocol_digest": _REMOVED_PROTOCOL_DIGEST,
                        "topology_fingerprint": _REMOVED_TOPOLOGY_FINGERPRINT,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO run_resume_operations (
                            run_id, operation_id, owner_id, tenant_id, task_id,
                            savepoint_id, command_fingerprint,
                            source_checkpoint_cursor, phase, attempt_count,
                            created_at, updated_at
                        ) VALUES (
                            'run-old-contract', 'resume-old-contract',
                            'owner-old-contract', 'tenant-old-contract',
                            'task-old-contract', 'savepoint-old-contract',
                            repeat('c', 64), CAST(:cursor AS jsonb), 'intent', 0,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"cursor": json.dumps(_cursor("checkpoint-old-contract"))},
                )

            command.upgrade(config, "20260809_0043")
            with scoped_engine.connect() as connection:
                assert connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one() == "20260809_0043"
                assert connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'checkpoint_projection_receipts' "
                        "AND column_name = 'schema_version'"
                    )
                ).scalar_one() == 0
                assert connection.execute(
                    text(
                        "SELECT status, execution_protocol_status, execution_protocol, "
                        "execution_protocol_digest, execution_protocol_quarantine_reason, "
                        "error FROM runs WHERE run_id = 'run-old-contract'"
                    )
                ).one() == (
                    "cancelled",
                    "quarantined",
                    None,
                    None,
                    "migration.checkpoint_graph_contract_removed",
                    "migration.checkpoint_graph_contract_removed",
                )
                assert connection.execute(
                    text(
                        "SELECT count(*) FROM checkpoint_links "
                        "WHERE run_id = 'run-old-contract'"
                    )
                ).scalar_one() == 0
                assert connection.execute(
                    text(
                        "SELECT count(*) FROM run_resume_operations "
                        "WHERE run_id = 'run-old-contract'"
                    )
                ).scalar_one() == 0
                assert connection.execute(
                    text("SELECT status FROM tasks WHERE task_id = 'task-old-contract'")
                ).scalar_one() == "cancelled"
        finally:
            scoped_engine.dispose()
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()
