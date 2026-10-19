from __future__ import annotations

from datetime import UTC, datetime
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.infrastructure.postgres.database import (
    Database,
    EXPECTED_SCHEMA_REVISION,
    create_database,
)
from CAD2BIM_backend.infrastructure.postgres.models import Base


EXPECTED_TABLES = {
    "users",
    "tasks",
    "runs",
    "run_events",
    "interrupts",
    "feedback_commands",
    "feedback_delivery_batches",
    "feedback_delivery_batch_members",
    "stage_plans",
    "artifacts",
    "artifact_versions",
    "ifc_run_projections",
    "ifc_run_stage_projections",
    "mcp_sessions",
    "mcp_snapshots",
    "checkpoint_links",
    "checkpoint_projection_receipts",
    "run_stage_business_views",
    "run_artifact_views",
    "geometry_resources",
    "geometry_resource_versions",
    "geometry_resource_version_assets",
    "geometry_resource_version_inputs",
    "geometry_query_attempts",
    "data_query_attempts",
    "data_query_inputs",
    "opaque_reference_index",
    "ifc_query_attempts",
    "ifc_query_inputs",
    "safe_point_publications",
    "run_resume_operations",
    "user_savepoint_commands",
    "principals",
    "tenants",
    "tenant_principals",
    "task_tenant_bindings",
    "identity_migration_quarantine",
    "direct_session_bindings",
    "data_packages",
    "data_package_members",
    "critical_operations",
    "critical_operation_inputs",
    "data_catalog_entries",
    "ifc_models",
    "ifc_model_revisions",
    "product_families",
    "product_family_versions",
    "product_family_geometry_bindings",
    "product_family_version_assets",
}

LEGACY_OWNER_TABLES = {
    name for name, table in Base.metadata.tables.items() if "owner_id" in table.c
}


def _unique_constraints(table_name: str) -> dict[str, tuple[str, ...]]:
    table = Base.metadata.tables[table_name]
    return {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint) and constraint.name is not None
    }


def _foreign_key_constraints(
    table_name: str,
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    table = Base.metadata.tables[table_name]
    return {
        constraint.name: (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint) and constraint.name is not None
    }


def _check_constraints(table_name: str) -> set[str]:
    return {
        constraint.name
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }


def test_metadata_contains_the_complete_control_plane_schema() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    assert len(LEGACY_OWNER_TABLES) == 33
    for table_name in LEGACY_OWNER_TABLES:
        assert "tenant_id" in Base.metadata.tables[table_name].c

    assert "content" not in Base.metadata.tables["artifacts"].c
    assert "content" not in Base.metadata.tables["artifact_versions"].c
    package_members = Base.metadata.tables["data_package_members"]
    assert package_members.c.json_kind.nullable is True
    assert package_members.c.json_kind.type.length == 16
    assert {
        "ck_data_package_members_json_kind_valid",
        "ck_data_package_members_member_contract_valid",
        "ck_data_package_members_member_kind_valid",
        "ck_data_package_members_payload_format_metadata_valid",
        "ck_data_package_members_payload_metadata_scope_valid",
    } <= _check_constraints("data_package_members")
    assert {
        "storage_key",
        "size_bytes",
        "sha256",
    } <= set(Base.metadata.tables["artifact_versions"].c.keys())
    checkpoint_ns = Base.metadata.tables["checkpoint_links"].c.checkpoint_ns
    assert checkpoint_ns.nullable is False
    assert checkpoint_ns.type.length == 1_024
    assert str(checkpoint_ns.server_default.arg) == "''"
    checkpoint_links = Base.metadata.tables["checkpoint_links"]
    assert {
        "execution_protocol_id",
        "execution_protocol_digest",
        "topology_fingerprint",
    }.isdisjoint(checkpoint_links.c.keys())
    assert {"run_id", "checkpoint_id", "checkpoint_ns"} <= set(checkpoint_links.c.keys())
    assert Base.metadata.tables["ifc_run_projections"].c.source_checkpoint_ns.type.length == 1_024
    publication = Base.metadata.tables["safe_point_publications"]
    assert publication.c.source_checkpoint_ns.type.length == 1_024
    assert publication.c.checkpoint_ns.type.length == 1_024
    assert publication.c.checkpoint_map.nullable is True
    assert publication.c.checkpoint_next.nullable is True
    resume_operation = Base.metadata.tables["run_resume_operations"]
    assert "cursor_schema_version" not in resume_operation.c
    assert "observed_checkpoint_id" not in resume_operation.c
    assert resume_operation.c.source_checkpoint_cursor.nullable is False
    assert resume_operation.c.observed_checkpoint_cursor.nullable is True
    assert {
        "feedback_id",
        "accepted_sequence",
        "author_principal_id",
        "client_request_id",
        "delivery_mode",
        "resolved_scope",
        "claimed_batch_id",
        "delivered_checkpoint_id",
        "delivered_at",
    } <= set(Base.metadata.tables["feedback_commands"].c.keys())
    feedback_sequence = Base.metadata.tables["feedback_commands"].c.accepted_sequence
    assert feedback_sequence.nullable is False
    assert str(feedback_sequence.type) == "BIGINT"
    assert {
        "batch_id",
        "target_fingerprint",
        "claim_high_watermark",
        "contains_interrupt",
        "claimed_writer_generation",
        "claimed_checkpoint_id",
        "claimed_checkpoint_cursor",
        "delivered_checkpoint_id",
        "delivered_at",
    } <= set(Base.metadata.tables["feedback_delivery_batches"].c.keys())
    assert {
        "batch_id",
        "feedback_id",
        "run_id",
        "accepted_sequence",
    } <= set(Base.metadata.tables["feedback_delivery_batch_members"].c.keys())
    assert "attributes" in Base.metadata.tables["run_events"].c


def test_nullable_json_artifact_references_bind_none_as_sql_null() -> None:
    """Paired Artifact columns must observe the same SQL NULL semantics."""

    dialect = postgresql.dialect()
    columns = (
        Base.metadata.tables["runs"].c.final_artifact,
        Base.metadata.tables["ifc_run_projections"].c.final_artifact,
        Base.metadata.tables["ifc_run_stage_projections"].c.accepted_artifact,
    )
    for column in columns:
        processor = column.type.dialect_impl(dialect).bind_processor(dialect)
        assert processor is not None
        assert processor(None) is None


def test_task_scoped_foreign_keys_cannot_cross_tenants_or_tasks() -> None:
    expected_links = {
        ("runs", "fk_runs_owner_task"): (
            ("owner_id", "task_id"),
            ("tasks.owner_id", "tasks.task_id"),
        ),
        ("runs", "fk_runs_final_artifact_scope"): (
            ("owner_id", "task_id", "run_id", "final_artifact_id"),
            (
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ),
        ),
        ("runs", "fk_runs_final_artifact_version"): (
            ("owner_id", "task_id", "final_artifact_id", "final_artifact_version_id"),
            (
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ),
        ),
        ("run_events", "fk_run_events_owner_task_run"): (
            ("owner_id", "task_id", "run_id"),
            ("runs.owner_id", "runs.task_id", "runs.run_id"),
        ),
        ("run_stage_business_views", "fk_stage_business_views_owner_task_run"): (
            ("owner_id", "task_id", "run_id"),
            ("runs.owner_id", "runs.task_id", "runs.run_id"),
        ),
        ("interrupts", "fk_interrupts_owner_task_run"): (
            ("owner_id", "task_id", "run_id"),
            ("runs.owner_id", "runs.task_id", "runs.run_id"),
        ),
        ("feedback_commands", "fk_feedback_commands_owner_task_run"): (
            ("owner_id", "task_id", "run_id"),
            ("runs.owner_id", "runs.task_id", "runs.run_id"),
        ),
        (
            "feedback_delivery_batches",
            "fk_feedback_delivery_batches_owner_task_run",
        ): (
            ("owner_id", "task_id", "run_id"),
            ("runs.owner_id", "runs.task_id", "runs.run_id"),
        ),
        (
            "feedback_delivery_batches",
            "fk_feedback_delivery_batches_tenant_task_tasks",
        ): (
            ("tenant_id", "task_id"),
            ("tasks.tenant_id", "tasks.task_id"),
        ),
        (
            "feedback_delivery_batch_members",
            "fk_feedback_delivery_batch_members_batch",
        ): (
            ("tenant_id", "run_id", "batch_id"),
            (
                "feedback_delivery_batches.tenant_id",
                "feedback_delivery_batches.run_id",
                "feedback_delivery_batches.batch_id",
            ),
        ),
        (
            "feedback_delivery_batch_members",
            "fk_feedback_delivery_batch_members_command",
        ): (
            ("tenant_id", "run_id", "feedback_id"),
            (
                "feedback_commands.tenant_id",
                "feedback_commands.run_id",
                "feedback_commands.feedback_id",
            ),
        ),
        ("stage_plans", "fk_stage_plans_owner_task_run"): (
            ("owner_id", "task_id", "run_id"),
            ("runs.owner_id", "runs.task_id", "runs.run_id"),
        ),
        ("artifacts", "fk_artifacts_owner_task_run"): (
            ("owner_id", "task_id", "run_id"),
            ("runs.owner_id", "runs.task_id", "runs.run_id"),
        ),
        ("artifacts", "fk_artifacts_owner_task_current_version"): (
            ("owner_id", "task_id", "artifact_id", "current_version_id"),
            (
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ),
        ),
        ("artifact_versions", "fk_artifact_versions_owner_task_artifact"): (
            ("owner_id", "task_id", "artifact_id"),
            ("artifacts.owner_id", "artifacts.task_id", "artifacts.artifact_id"),
        ),
        ("mcp_sessions", "fk_mcp_sessions_owner_task_run"): (
            ("owner_id", "task_id", "run_id"),
            ("runs.owner_id", "runs.task_id", "runs.run_id"),
        ),
        ("mcp_snapshots", "fk_mcp_snapshots_owner_task_session"): (
            ("owner_id", "task_id", "session_id"),
            ("mcp_sessions.owner_id", "mcp_sessions.task_id", "mcp_sessions.session_id"),
        ),
        ("mcp_snapshots", "fk_mcp_snapshots_owner_task_artifact_version"): (
            ("owner_id", "task_id", "artifact_version_id"),
            (
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.version_id",
            ),
        ),
        ("checkpoint_links", "fk_checkpoint_links_owner_task_run"): (
            ("owner_id", "task_id", "run_id"),
            ("runs.owner_id", "runs.task_id", "runs.run_id"),
        ),
        ("checkpoint_links", "fk_checkpoint_links_owner_task_mcp_session"): (
            ("owner_id", "task_id", "mcp_session_id"),
            ("mcp_sessions.owner_id", "mcp_sessions.task_id", "mcp_sessions.session_id"),
        ),
        ("checkpoint_links", "fk_checkpoint_links_owner_task_mcp_snapshot"): (
            ("owner_id", "task_id", "mcp_session_id", "mcp_snapshot_id"),
            (
                "mcp_snapshots.owner_id",
                "mcp_snapshots.task_id",
                "mcp_snapshots.session_id",
                "mcp_snapshots.snapshot_id",
            ),
        ),
        ("runs", "fk_runs_owner_task_resume_savepoint"): (
            ("owner_id", "task_id", "resume_from_savepoint_id"),
            (
                "checkpoint_links.owner_id",
                "checkpoint_links.task_id",
                "checkpoint_links.savepoint_id",
            ),
        ),
        ("run_resume_operations", "fk_run_resume_operations_owner_task_run"): (
            ("owner_id", "task_id", "run_id"),
            ("runs.owner_id", "runs.task_id", "runs.run_id"),
        ),
        ("run_resume_operations", "fk_run_resume_operations_owner_task_savepoint"): (
            ("owner_id", "task_id", "savepoint_id"),
            (
                "checkpoint_links.owner_id",
                "checkpoint_links.task_id",
                "checkpoint_links.savepoint_id",
            ),
        ),
        ("run_resume_operations", "fk_run_resume_operations_owner_task_mcp_session"): (
            ("owner_id", "task_id", "target_mcp_session_id"),
            (
                "mcp_sessions.owner_id",
                "mcp_sessions.task_id",
                "mcp_sessions.session_id",
            ),
        ),
        ("safe_point_publications", "fk_safe_point_publications_owner_task"): (
            ("owner_id", "task_id"),
            ("tasks.owner_id", "tasks.task_id"),
        ),
        ("safe_point_publications", "fk_safe_point_publications_owner_task_run"): (
            ("owner_id", "task_id", "run_id"),
            ("runs.owner_id", "runs.task_id", "runs.run_id"),
        ),
        (
            "safe_point_publications",
            "fk_safe_point_publications_owner_task_mcp_session",
        ): (
            ("owner_id", "task_id", "mcp_session_id"),
            ("mcp_sessions.owner_id", "mcp_sessions.task_id", "mcp_sessions.session_id"),
        ),
        (
            "safe_point_publications",
            "fk_safe_point_publications_owner_task_mcp_snapshot",
        ): (
            ("owner_id", "task_id", "mcp_session_id", "mcp_snapshot_id"),
            (
                "mcp_snapshots.owner_id",
                "mcp_snapshots.task_id",
                "mcp_snapshots.session_id",
                "mcp_snapshots.snapshot_id",
            ),
        ),
        (
            "safe_point_publications",
            "fk_safe_point_publications_owner_task_artifact_version",
        ): (
            ("owner_id", "task_id", "artifact_id", "artifact_version_id"),
            (
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ),
        ),
        (
            "safe_point_publications",
            "fk_safe_point_publications_owner_task_savepoint",
        ): (
            ("owner_id", "task_id", "savepoint_id"),
            (
                "checkpoint_links.owner_id",
                "checkpoint_links.task_id",
                "checkpoint_links.savepoint_id",
            ),
        ),
        ("ifc_run_projections", "fk_ifc_run_projections_owner_task_run"): (
            ("owner_id", "task_id", "run_id"),
            ("runs.owner_id", "runs.task_id", "runs.run_id"),
        ),
        ("ifc_run_projections", "fk_ifc_run_projections_final_artifact_scope"): (
            ("owner_id", "task_id", "run_id", "final_artifact_id"),
            (
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ),
        ),
        ("ifc_run_projections", "fk_ifc_run_projections_final_artifact_version"): (
            ("owner_id", "task_id", "final_artifact_id", "final_artifact_version_id"),
            (
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ),
        ),
        ("ifc_run_stage_projections", "fk_ifc_run_stage_projections_run_projection"): (
            ("owner_id", "task_id", "run_id"),
            (
                "ifc_run_projections.owner_id",
                "ifc_run_projections.task_id",
                "ifc_run_projections.run_id",
            ),
        ),
        ("ifc_run_stage_projections", "fk_ifc_run_stage_projections_stage_plan_scope"): (
            (
                "owner_id",
                "task_id",
                "run_id",
                "stage_key",
                "stage_plan_revision",
                "stage_plan_id",
            ),
            (
                "stage_plans.owner_id",
                "stage_plans.task_id",
                "stage_plans.run_id",
                "stage_plans.stage_key",
                "stage_plans.definition_revision",
                "stage_plans.plan_id",
            ),
        ),
        (
            "ifc_run_stage_projections",
            "fk_ifc_stage_projection_accepted_artifact_scope",
        ): (
            (
                "owner_id",
                "task_id",
                "accepted_artifact_run_id",
                "accepted_artifact_id",
            ),
            (
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ),
        ),
        (
            "ifc_run_stage_projections",
            "fk_ifc_stage_projection_accepted_artifact_version",
        ): (
            (
                "owner_id",
                "task_id",
                "accepted_artifact_id",
                "accepted_artifact_version_id",
            ),
            (
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ),
        ),
        (
            "ifc_run_stage_projections",
            "fk_ifc_stage_projection_accepted_savepoint",
        ): (
            ("owner_id", "task_id", "accepted_savepoint_id"),
            (
                "checkpoint_links.owner_id",
                "checkpoint_links.task_id",
                "checkpoint_links.savepoint_id",
            ),
        ),
    }
    for (table_name, constraint_name), expected in expected_links.items():
        assert _foreign_key_constraints(table_name)[constraint_name] == expected


def test_task_scoped_run_foreign_key_rejects_a_cross_task_reference() -> None:
    engine = create_engine("sqlite:///:memory:")
    required_tables = {"run_events"}
    while True:
        parents = {
            foreign_key.column.table.name
            for name in required_tables
            for foreign_key in Base.metadata.tables[name].foreign_keys
        }
        if parents <= required_tables:
            break
        required_tables.update(parents)
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        Base.metadata.create_all(
            connection, tables=[Base.metadata.tables[name] for name in required_tables]
        )
        connection.execute(
            Base.metadata.tables["tenants"].insert(),
            {"tenant_id": "tenant-1", "kind": "personal", "status": "active"},
        )
        connection.execute(
            Base.metadata.tables["users"].insert(),
            {"owner_id": "owner-1", "tenant_id": "tenant-1"},
        )
        connection.execute(
            Base.metadata.tables["tasks"].insert(),
            [
                {
                    "task_id": "task-a",
                    "owner_id": "owner-1",
                    "tenant_id": "tenant-1",
                    "title": "A",
                    "metadata_json": {},
                    "status": "draft",
                    "active_module": "ifc",
                },
                {
                    "task_id": "task-b",
                    "owner_id": "owner-1",
                    "tenant_id": "tenant-1",
                    "title": "B",
                    "metadata_json": {},
                    "status": "draft",
                    "active_module": "ifc",
                },
            ],
        )
        connection.execute(
            Base.metadata.tables["runs"].insert(),
            {
                "run_id": "run-a",
                "owner_id": "owner-1",
                "tenant_id": "tenant-1",
                "task_id": "task-a",
                "module": "ifc",
                "stage_key": "default",
                "input_payload": {},
                "idempotency_key": "run-key",
                "request_fingerprint": "a" * 64,
                "thread_id": "thread-a",
                "execution_protocol_status": "quarantined",
                "execution_protocol_quarantine_reason": "test.fixture",
                "status": "queued",
            },
        )
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.execute(
                Base.metadata.tables["run_events"].insert(),
                {
                    "event_id": "event-a",
                    "source_event_id": "source-a",
                    "source_fingerprint": "b" * 64,
                    "sequence": 1,
                    "owner_id": "owner-1",
                    "tenant_id": "tenant-1",
                    "task_id": "task-b",
                    "run_id": "run-a",
                    "occurred_at": datetime.now(UTC),
                    "scope": {},
                    "event": "running",
                    "event_type": "run.status.changed",
                    "audience": "public",
                    "item_id": "run-a",
                    "parent_item_id": None,
                    "subject_refs": [],
                    "metrics": {},
                    "attributes": {},
                },
            )


def test_sequences_versions_checkpoint_links_and_idempotency_are_unique() -> None:
    expected = {
        "tasks": {
            "uq_tasks_owner_idempotency": ("owner_id", "idempotency_key"),
        },
        "runs": {
            "uq_runs_owner_task_run": ("owner_id", "task_id", "run_id"),
            "uq_runs_task_idempotency": ("task_id", "idempotency_key"),
        },
        "run_events": {
            "uq_run_events_run_sequence": ("run_id", "sequence"),
            "uq_run_events_run_source": ("run_id", "source_event_id"),
        },
        "feedback_commands": {
            "uq_feedback_commands_idempotency": (
                "tenant_id",
                "author_principal_id",
                "client_request_id",
            ),
            "uq_feedback_commands_run_sequence": ("run_id", "accepted_sequence"),
            "uq_feedback_commands_tenant_run_feedback": (
                "tenant_id",
                "run_id",
                "feedback_id",
            ),
        },
        "feedback_delivery_batches": {
            "uq_feedback_delivery_batches_tenant_run_batch": (
                "tenant_id",
                "run_id",
                "batch_id",
            ),
            "uq_feedback_delivery_batches_replay": (
                "run_id",
                "target_fingerprint",
                "claim_high_watermark",
                "claimed_writer_generation",
            ),
        },
        "feedback_delivery_batch_members": {
            "uq_feedback_delivery_batch_members_sequence": (
                "batch_id",
                "accepted_sequence",
            ),
        },
        "artifact_versions": {
            "uq_artifact_versions_owner_task_version": (
                "owner_id",
                "task_id",
                "version_id",
            ),
            "uq_artifact_versions_artifact_version": ("artifact_id", "version_no"),
        },
        "mcp_snapshots": {
            "uq_mcp_snapshots_owner_task_session_snapshot": (
                "owner_id",
                "task_id",
                "session_id",
                "snapshot_id",
            ),
        },
        "checkpoint_links": {
            "uq_checkpoint_links_owner_task_savepoint": (
                "owner_id",
                "task_id",
                "savepoint_id",
            ),
            "uq_checkpoint_links_run_checkpoint_cursor": (
                "run_id",
                "checkpoint_ns",
                "checkpoint_id",
            ),
            "uq_checkpoint_links_run_idempotency": ("run_id", "idempotency_key"),
        },
        "safe_point_publications": {
            "uq_safe_point_publications_run_idempotency": (
                "run_id",
                "idempotency_key",
            ),
            "uq_safe_point_publications_run_checkpoint_cursor": (
                "run_id",
                "checkpoint_ns",
                "checkpoint_id",
            ),
            "uq_safe_point_publications_savepoint": ("savepoint_id",),
        },
        "run_resume_operations": {
            "uq_run_resume_operations_operation_id": ("operation_id",),
        },
        "stage_plans": {
            "uq_stage_plans_projection_scope": (
                "owner_id",
                "task_id",
                "run_id",
                "stage_key",
                "definition_revision",
                "plan_id",
            ),
        },
        "artifacts": {
            "uq_artifacts_owner_task_run_artifact": (
                "owner_id",
                "task_id",
                "run_id",
                "artifact_id",
            ),
        },
        "ifc_run_projections": {
            "uq_ifc_run_projections_owner_task_run": (
                "owner_id",
                "task_id",
                "run_id",
            ),
        },
        "ifc_run_stage_projections": {
            "uq_ifc_run_stage_projections_owner_task_run_stage": (
                "owner_id",
                "task_id",
                "run_id",
                "stage_key",
            ),
        },
    }
    for table_name, required in expected.items():
        constraints = _unique_constraints(table_name)
        for name, columns in required.items():
            assert constraints[name] == columns

    assert "uq_stage_plans_owner_plan_version" not in _unique_constraints("stage_plans")


def test_database_checks_statuses_single_pending_interrupt_and_writer_lease_triplet() -> None:
    required_checks = {
        "tasks": "ck_tasks_status_valid",
        "runs": "ck_runs_status_valid",
        "feedback_commands": "ck_feedback_commands_status_valid",
        "interrupts": "ck_interrupts_status_valid",
        "artifacts": "ck_artifacts_status_valid",
        "mcp_sessions": "ck_mcp_sessions_status_valid",
    }
    for table_name, check_name in required_checks.items():
        assert check_name in _check_constraints(table_name)

    assert {
        "ck_feedback_commands_accepted_sequence_positive",
        "ck_feedback_commands_delivery_mode_valid",
        "ck_feedback_commands_lifecycle_consistent",
        "ck_feedback_commands_status_valid",
    } <= _check_constraints("feedback_commands")
    assert {
            "ck_feedback_delivery_batches_delivered_fields_consistent",
        "ck_feedback_delivery_batches_watermark_positive",
        "ck_feedback_delivery_batches_writer_generation_positive",
    } <= _check_constraints("feedback_delivery_batches")
    assert {
        "ck_feedback_delivery_batch_members_accepted_sequence_positive",
    } <= _check_constraints("feedback_delivery_batch_members")
    assert {
        "ck_runs_execution_protocol_mapping_valid",
        "ck_runs_final_artifact_fields_consistent",
    } <= _check_constraints("runs")
    assert {
        "ck_runs_execution_protocol_digest_shape",
        "ck_runs_execution_protocol_identity_consistent",
    }.isdisjoint(_check_constraints("runs"))

    assert {
        "ck_mcp_sessions_writer_lease_token_pair",
        "ck_mcp_sessions_writer_lease_expiry_pair",
    } <= _check_constraints("mcp_sessions")
    assert {
        "ck_checkpoint_links_mcp_reference_pair",
    } <= _check_constraints("checkpoint_links")
    assert {
        "ck_checkpoint_links_execution_protocol_binding_complete",
        "ck_checkpoint_links_execution_protocol_digest_shape",
        "ck_checkpoint_links_topology_fingerprint_shape",
    }.isdisjoint(_check_constraints("checkpoint_links"))
    assert {
        "ck_run_resume_operations_attempt_count_nonnegative",
        "ck_run_resume_operations_exact_cursor_phase_consistent",
        "ck_run_resume_operations_observation_pair",
        "ck_run_resume_operations_observed_outcome_valid",
        "ck_run_resume_operations_phase_observation_consistent",
        "ck_run_resume_operations_phase_valid",
    } <= _check_constraints("run_resume_operations")
    assert {
        "ck_safe_point_publications_checkpoint_fields_consistent",
        "ck_safe_point_publications_checkpoint_digest_length",
        "ck_safe_point_publications_checksum_length",
        "ck_safe_point_publications_phase_fields_consistent",
        "ck_safe_point_publications_phase_valid",
        "ck_safe_point_publications_source_cursor_consistent",
    } <= _check_constraints("safe_point_publications")
    assert {
        "ck_ifc_run_projections_final_artifact_fields_consistent",
        "ck_ifc_run_projections_final_artifact_only_when_passed",
        "ck_ifc_run_projections_passed_fields_present",
        "ck_ifc_run_projections_revision_positive",
        "ck_ifc_run_projections_source_checkpoint_id_nonempty",
        "ck_ifc_run_projections_status_valid",
    } <= _check_constraints("ifc_run_projections")
    assert {
        "ck_ifc_run_stage_projections_accepted_artifact_pair",
        "ck_ifc_run_stage_projections_accepted_tree_fields_consistent",
        "ck_ifc_run_stage_projections_accepted_tree_fingerprint_shape",
        "ck_ifc_run_stage_projections_accepted_tree_revision_positive",
        "ck_ifc_run_stage_projections_passed_fields_present",
        "ck_ifc_run_stage_projections_revision_positive",
        "ck_ifc_run_stage_projections_status_valid",
    } <= _check_constraints("ifc_run_stage_projections")

    pending_index = Base.metadata.tables["interrupts"].indexes
    matching = [index for index in pending_index if index.name == "uq_interrupts_run_pending"]
    assert len(matching) == 1
    assert matching[0].unique is True
    assert tuple(column.name for column in matching[0].columns) == ("run_id",)
    assert str(matching[0].dialect_options["postgresql"]["where"]) == "status = 'pending'"

    feedback_indexes = Base.metadata.tables["feedback_commands"].indexes
    sequence_indexes = [
        index
        for index in feedback_indexes
        if index.name == "ix_feedback_commands_owner_run_sequence"
    ]
    assert len(sequence_indexes) == 1
    assert tuple(column.name for column in sequence_indexes[0].columns) == (
        "owner_id",
        "run_id",
        "accepted_sequence",
    )


def test_json_columns_compile_to_jsonb_for_postgresql() -> None:
    dialect = postgresql.dialect()
    json_columns = (
        Base.metadata.tables["runs"].c.input_payload,
        Base.metadata.tables["runs"].c.final_artifact,
        Base.metadata.tables["run_events"].c.scope,
        Base.metadata.tables["interrupts"].c.payload,
        Base.metadata.tables["stage_plans"].c.plan,
        Base.metadata.tables["checkpoint_links"].c.artifact_ids,
        Base.metadata.tables["safe_point_publications"].c.checkpoint_map,
        Base.metadata.tables["safe_point_publications"].c.checkpoint_next,
        Base.metadata.tables["ifc_run_projections"].c.global_plan,
        Base.metadata.tables["ifc_run_stage_projections"].c.accepted_artifact,
    )
    assert all(column.type.compile(dialect=dialect) == "JSONB" for column in json_columns)


def test_application_revision_is_the_single_alembic_head() -> None:
    root = Path(__file__).resolve().parents[3]
    script = ScriptDirectory.from_config(Config(root / "alembic.ini"))
    assert script.get_heads() == [EXPECTED_SCHEMA_REVISION]


def test_publication_request_fingerprint_is_not_a_unique_business_identity() -> None:
    operations = Base.metadata.tables["critical_operations"]
    index = next(index for index in operations.indexes
                 if index.name == "uq_critical_operations_run_tool_fingerprint")
    assert index.unique
    assert tuple(column.name for column in index.columns) == (
        "run_id", "tool_name", "request_fingerprint",
    )
    expected_kinds = (
        "operation_kind IN ('source_ingestion', 'data_transform', "
        "'geometry_resource_write', 'product_family_write')"
    )
    assert str(index.dialect_options["postgresql"]["where"]) == expected_kinds
    assert str(index.dialect_options["sqlite"]["where"]) == expected_kinds


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revisions",
    [
        (EXPECTED_SCHEMA_REVISION,),
        (),
        ("20261001_0102",),
        ("20261001_0101",),
        ("20260927_0091",),
        (EXPECTED_SCHEMA_REVISION, "20260927_0091"),
    ],
)
async def test_database_readiness_requires_exactly_the_current_revision(revisions) -> None:
    class Connection:
        async def execute(self, statement):
            assert str(statement) == "SELECT 1"

        async def scalars(self, statement):
            assert str(statement) == "SELECT version_num FROM alembic_version"
            return SimpleNamespace(all=lambda: list(revisions))

    @asynccontextmanager
    async def connect():
        yield Connection()

    database = Database(engine=SimpleNamespace(connect=connect), session_factory=None)
    if revisions == (EXPECTED_SCHEMA_REVISION,):
        await database.verify_ready()
    else:
        with pytest.raises(RuntimeError, match="database schema revision"):
            await database.verify_ready()


def test_settings_openai_key_uses_dotenv_then_environment_without_public_repr(tmp_path) -> None:
    env_file = tmp_path / "count.env"
    env_file.write_text("OPENAI_API_KEY=synthetic-dotenv-key\n", encoding="utf-8")
    dotenv = Settings.load({"CAD2BIM_ENV_FILE": str(env_file)})
    environment = Settings.load({"CAD2BIM_ENV_FILE": str(env_file), "OPENAI_API_KEY": "synthetic-environment-key"})
    assert dotenv.openai_api_key == "synthetic-dotenv-key"
    assert environment.openai_api_key == "synthetic-environment-key"
    assert "synthetic" not in repr(dotenv) and "synthetic" not in repr(environment)
    assert "openai_api_key" not in repr(environment)


@pytest.mark.asyncio
async def test_database_factory_is_lazy_and_disposable() -> None:
    database = create_database(
        Settings(database_url="postgresql+psycopg://user:secret@example.invalid/cad2bim")
    )
    assert database.session_factory.kw["expire_on_commit"] is False
    assert database.engine.url.drivername == "postgresql+psycopg"
    await database.dispose()


def test_database_factory_rejects_missing_or_synchronous_driver() -> None:
    with pytest.raises(ValueError, match="CAD2BIM_DATABASE_URL is required"):
        create_database(Settings(database_url=None))
    with pytest.raises(ValueError, match=r"postgresql\+psycopg"):
        create_database(Settings(database_url="postgresql://user:secret@example.invalid/db"))
    with pytest.raises(ValueError, match=r"postgresql\+psycopg"):
        create_database(
            Settings(database_url="postgresql+asyncpg://user:secret@example.invalid/db")
        )


def test_initial_alembic_migration_renders_offline_without_a_database() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://user:secret@example.invalid/cad2bim"
    )
    output = StringIO()
    config.output_buffer = output

    command.upgrade(config, "head", sql=True)

    sql = output.getvalue()
    for table_name in EXPECTED_TABLES:
        assert f"CREATE TABLE {table_name}" in sql
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert (
                f"\n    {column.name} " in sql
                or f"ALTER TABLE {table.name} ADD COLUMN {column.name} " in sql
                or (f"ALTER TABLE {table.name} RENAME " in sql and f" TO {column.name};" in sql)
            )
        for constraint in table.constraints:
            if constraint.name:
                emitted_name = postgresql.dialect().identifier_preparer.format_constraint(
                    constraint
                )
                assert emitted_name in sql
        for index in table.indexes:
            assert index.name in sql
    assert "input_payload JSONB NOT NULL" in sql
    assert "CONSTRAINT uq_run_events_run_sequence UNIQUE (run_id, sequence)" in sql
    assert "CONSTRAINT uq_artifact_versions_artifact_version" in sql
    assert "ALTER COLUMN checkpoint_ns TYPE VARCHAR(1024)" in sql
    assert "CREATE TABLE safe_point_publications" in sql
    assert "CONSTRAINT uq_checkpoint_links_run_checkpoint_cursor" in sql
    assert "ALTER TABLE checkpoint_links DROP COLUMN execution_protocol_id" in sql
    assert "ALTER TABLE checkpoint_links DROP COLUMN execution_protocol_digest" in sql
    assert "ALTER TABLE checkpoint_links DROP COLUMN topology_fingerprint" in sql
    assert "DROP TRIGGER trg_checkpoint_links_execution_protocol_immutable" in sql
    assert "CONSTRAINT uq_safe_point_publications_run_checkpoint_cursor" in sql
    assert "CONSTRAINT fk_runs_owner_task_resume_savepoint" in sql
    assert "CONSTRAINT fk_artifacts_owner_task_current_version" in sql
    assert "CONSTRAINT uq_runs_owner_task_run UNIQUE (owner_id, task_id, run_id)" in sql
    assert "CREATE TABLE run_resume_operations" in sql
    assert "CONSTRAINT uq_run_resume_operations_operation_id UNIQUE (operation_id)" in sql
    assert "ADD COLUMN cursor_schema_version VARCHAR(16)" in sql
    assert "ADD COLUMN source_checkpoint_cursor JSONB" in sql
    assert "ADD COLUMN observed_checkpoint_cursor JSONB" in sql
    assert "DROP COLUMN observed_checkpoint_id" in sql
    assert "DROP COLUMN cursor_schema_version" in sql
    assert "ck_run_resume_operations_exact_cursor_phase_consistent" in sql
    assert "ADD COLUMN delivery_id VARCHAR(255)" in sql
    assert "CONSTRAINT uq_feedback_run_delivery UNIQUE (run_id, delivery_id)" in sql
    assert "ALTER TABLE feedback ADD COLUMN sequence BIGINT" in sql
    assert "row_number() OVER (PARTITION BY run_id ORDER BY created_at, feedback_id)" in sql
    assert "ALTER TABLE feedback ALTER COLUMN sequence SET NOT NULL" in sql
    assert "CONSTRAINT uq_feedback_run_sequence UNIQUE (run_id, sequence)" in sql
    assert "SET acknowledged_by_checkpoint_id = consumed_by_checkpoint_id" in sql
    assert "ADD COLUMN acknowledged_by_checkpoint_thread_id VARCHAR(255)" in sql
    assert "ADD COLUMN acknowledged_by_checkpoint_ns VARCHAR(1024)" in sql
    assert "ADD COLUMN acknowledged_by_checkpoint_map JSONB" in sql
    assert "ck_feedback_exact_acknowledgement_cursor_consistent" in sql
    assert "CREATE UNIQUE INDEX uq_interrupts_run_pending" in sql
    assert "WHERE status = 'pending'" in sql
    assert "ADD COLUMN execution_protocol_status VARCHAR(32)" in sql
    assert "ADD COLUMN execution_protocol JSONB" in sql
    assert "ALTER TABLE runs DROP COLUMN execution_protocol_id" in sql
    assert "ALTER TABLE runs DROP COLUMN execution_protocol_digest" in sql
    assert "ALTER TABLE runs ALTER COLUMN execution_protocol_status DROP DEFAULT" in sql
    assert "ck_runs_execution_protocol_mapping_valid" in sql
    assert "DROP CONSTRAINT ck_runs_ck_runs_execution_protocol_identity_consistent" in sql
    assert "DROP CONSTRAINT ck_runs_ck_runs_execution_protocol_digest_shape" in sql
    assert "CREATE FUNCTION prevent_pinned_execution_protocol_mutation()" in sql
    assert "CREATE TRIGGER trg_runs_pinned_execution_protocol_immutable" in sql
