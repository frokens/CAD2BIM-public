"""Create the CAD2BIM control-plane schema.

Revision ID: 20260711_0001
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260711_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _json(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=500), nullable=True),
        _updated_at(),
        _created_at(),
        sa.PrimaryKeyConstraint("owner_id", name=op.f("pk_users")),
    )
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=True),
        _json("metadata_json"),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("active_module", sa.String(length=64), nullable=False),
        sa.Column("workspace_uri", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
        _updated_at(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.owner_id"],
            name=op.f("fk_tasks_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(idempotency_key IS NULL) = (request_fingerprint IS NULL)",
            name=op.f("ck_tasks_idempotency_fingerprint_pair"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'running', 'paused', 'waiting_user', "
            "'completed', 'failed', 'cancelled', 'archived')",
            name=op.f("ck_tasks_status_valid"),
        ),
        sa.PrimaryKeyConstraint("task_id", name=op.f("pk_tasks")),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_tasks_owner_idempotency"),
        sa.UniqueConstraint("owner_id", "task_id", name="uq_tasks_owner_task"),
    )
    op.create_index("ix_tasks_owner_created", "tasks", ["owner_id", "created_at"], unique=False)
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("module", sa.String(length=64), nullable=False),
        sa.Column("stage_key", sa.String(length=255), nullable=False),
        _json("input_payload"),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("claimed_by", sa.String(length=255), nullable=True),
        sa.Column("lease_token", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("pause_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "feedback_pending", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("resume_from_savepoint_id", sa.String(length=80), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _json("plan", nullable=True),
        _json("business_tree", nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        _updated_at(),
        _created_at(),
        sa.CheckConstraint("claim_count >= 0", name=op.f("ck_runs_claim_count_nonnegative")),
        sa.CheckConstraint(
            "(claimed_by IS NULL) = (lease_expires_at IS NULL)",
            name=op.f("ck_runs_claim_lease_expiry_pair"),
        ),
        sa.CheckConstraint(
            "(claimed_by IS NULL) = (lease_token IS NULL)",
            name=op.f("ck_runs_claim_lease_token_pair"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'waiting_user', "
            "'completed', 'failed', 'cancelled')",
            name=op.f("ck_runs_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_runs_owner_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_runs")),
        sa.UniqueConstraint("owner_id", "task_id", "run_id", name="uq_runs_owner_task_run"),
        sa.UniqueConstraint("task_id", "idempotency_key", name="uq_runs_task_idempotency"),
    )
    op.create_index(
        "ix_runs_claim_queue",
        "runs",
        ["status", "lease_expires_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_runs_owner_task_created",
        "runs",
        ["owner_id", "task_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_runs_task_active",
        "runs",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'paused', 'waiting_user')"),
    )
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_version_id", sa.String(length=80), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        _updated_at(),
        _created_at(),
        sa.CheckConstraint(
            "status IN ('created', 'uploading', 'ready', 'available', "
            "'failed', 'archived', 'deleted')",
            name=op.f("ck_artifacts_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_artifacts_owner_task_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_artifacts_owner_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("artifact_id", name=op.f("pk_artifacts")),
        sa.UniqueConstraint(
            "owner_id", "task_id", "artifact_id", name="uq_artifacts_owner_task_artifact"
        ),
        sa.UniqueConstraint("task_id", "idempotency_key", name="uq_artifacts_task_idempotency"),
    )
    op.create_index(
        "ix_artifacts_owner_task_created",
        "artifacts",
        ["owner_id", "task_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "feedback",
        sa.Column("feedback_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.String(length=32), nullable=False),
        sa.Column("target_ref", sa.String(length=255), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("consumed_by_checkpoint_id", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_feedback_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_feedback_owner_task",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(idempotency_key IS NULL) = (request_fingerprint IS NULL)",
            name=op.f("ck_feedback_idempotency_fingerprint_pair"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'consumed', 'ignored', 'superseded')",
            name=op.f("ck_feedback_status_valid"),
        ),
        sa.PrimaryKeyConstraint("feedback_id", name=op.f("pk_feedback")),
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_feedback_run_idempotency"),
    )
    op.create_index(
        "ix_feedback_owner_run_status",
        "feedback",
        ["owner_id", "run_id", "status"],
        unique=False,
    )
    op.create_table(
        "interrupts",
        sa.Column("interrupt_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        _json("payload"),
        sa.Column("status", sa.String(length=32), nullable=False),
        _json("response", nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("response_idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "status IN ('pending', 'responded')",
            name=op.f("ck_interrupts_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_interrupts_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_interrupts_owner_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("interrupt_id", name=op.f("pk_interrupts")),
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_interrupts_run_idempotency"),
        sa.UniqueConstraint(
            "run_id",
            "response_idempotency_key",
            name="uq_interrupts_run_response_idempotency",
        ),
    )
    op.create_index(
        "ix_interrupts_owner_run_status",
        "interrupts",
        ["owner_id", "run_id", "status"],
        unique=False,
    )
    op.create_index(
        "uq_interrupts_run_pending",
        "interrupts",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "mcp_sessions",
        sa.Column("session_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=True),
        sa.Column("server_name", sa.String(length=64), nullable=False),
        sa.Column("active_filename", sa.String(length=500), nullable=True),
        sa.Column("session_storage_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("writer_id", sa.String(length=255), nullable=True),
        sa.Column("writer_lease_token", sa.String(length=255), nullable=True),
        sa.Column("writer_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        _updated_at(),
        _created_at(),
        sa.CheckConstraint(
            "status IN ('active', 'closed', 'failed')",
            name=op.f("ck_mcp_sessions_status_valid"),
        ),
        sa.CheckConstraint(
            "(writer_id IS NULL) = (writer_lease_expires_at IS NULL)",
            name=op.f("ck_mcp_sessions_writer_lease_expiry_pair"),
        ),
        sa.CheckConstraint(
            "(writer_id IS NULL) = (writer_lease_token IS NULL)",
            name=op.f("ck_mcp_sessions_writer_lease_token_pair"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_mcp_sessions_owner_task_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_mcp_sessions_owner_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_mcp_sessions")),
        sa.UniqueConstraint(
            "owner_id", "task_id", "session_id", name="uq_mcp_sessions_owner_task_session"
        ),
        sa.UniqueConstraint("task_id", "idempotency_key", name="uq_mcp_sessions_task_idempotency"),
    )
    op.create_index(
        "ix_mcp_sessions_owner_task_status",
        "mcp_sessions",
        ["owner_id", "task_id", "status"],
        unique=False,
    )
    op.create_table(
        "run_events",
        sa.Column("event_id", sa.String(length=80), nullable=False),
        sa.Column("source_event_id", sa.String(length=80), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        _json("scope"),
        _json("node"),
        sa.Column("event", sa.String(length=32), nullable=False),
        _json("subject_refs"),
        _json("metrics"),
        sa.Column("payload_ref", sa.Text(), nullable=True),
        _created_at(),
        sa.CheckConstraint("sequence > 0", name=op.f("ck_run_events_sequence_positive")),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_run_events_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_run_events_owner_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_run_events")),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
        sa.UniqueConstraint("run_id", "source_event_id", name="uq_run_events_run_source"),
    )
    op.create_index(
        "ix_run_events_owner_run_sequence",
        "run_events",
        ["owner_id", "run_id", "sequence"],
        unique=False,
    )
    op.create_table(
        "stage_plans",
        sa.Column("stage_plan_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("plan_id", sa.String(length=80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("stage_key", sa.String(length=255), nullable=False),
        sa.Column("template_id", sa.String(length=255), nullable=True),
        _json("plan"),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_stage_plans_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_stage_plans_owner_task",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_stage_plans_version_positive")),
        sa.PrimaryKeyConstraint("stage_plan_id", name=op.f("pk_stage_plans")),
        sa.UniqueConstraint(
            "run_id", "stage_key", "version", name="uq_stage_plans_run_stage_version"
        ),
    )
    op.create_table(
        "artifact_versions",
        sa.Column("version_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("artifact_id", sa.String(length=80), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        _json("metadata_json"),
        _json("lineage_json"),
        _created_at(),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_artifact_versions_size_nonnegative")),
        sa.CheckConstraint("version_no > 0", name=op.f("ck_artifact_versions_version_positive")),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "artifact_id"],
            ["artifacts.owner_id", "artifacts.task_id", "artifacts.artifact_id"],
            name="fk_artifact_versions_owner_task_artifact",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("version_id", name=op.f("pk_artifact_versions")),
        sa.UniqueConstraint(
            "artifact_id", "version_no", name="uq_artifact_versions_artifact_version"
        ),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "artifact_id",
            "version_id",
            name="uq_artifact_versions_owner_task_artifact_version_id",
        ),
        sa.UniqueConstraint(
            "owner_id", "task_id", "version_id", name="uq_artifact_versions_owner_task_version"
        ),
    )
    op.create_table(
        "mcp_snapshots",
        sa.Column("snapshot_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("session_id", sa.String(length=80), nullable=False),
        sa.Column("stage_key", sa.String(length=255), nullable=True),
        sa.Column("snapshot_type", sa.String(length=64), nullable=False),
        sa.Column("artifact_version_id", sa.String(length=80), nullable=False),
        sa.Column("checksum_algorithm", sa.String(length=16), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        _json("snapshot_metadata"),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "checksum_algorithm IN ('sha256', 'md5')",
            name=op.f("ck_mcp_snapshots_checksum_algorithm_supported"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.version_id",
            ],
            name="fk_mcp_snapshots_owner_task_artifact_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "session_id"],
            ["mcp_sessions.owner_id", "mcp_sessions.task_id", "mcp_sessions.session_id"],
            name="fk_mcp_snapshots_owner_task_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_mcp_snapshots_owner_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_mcp_snapshots")),
        sa.UniqueConstraint(
            "owner_id", "task_id", "snapshot_id", name="uq_mcp_snapshots_owner_task_snapshot"
        ),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "session_id",
            "snapshot_id",
            name="uq_mcp_snapshots_owner_task_session_snapshot",
        ),
        sa.UniqueConstraint(
            "session_id", "idempotency_key", name="uq_mcp_snapshots_session_idempotency"
        ),
    )
    op.create_table(
        "checkpoint_links",
        sa.Column("savepoint_id", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=False),
        sa.Column("safe_point_key", sa.String(length=255), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=255), nullable=False),
        sa.Column("stage_key", sa.String(length=255), nullable=False),
        _json("checkpoint_next"),
        sa.Column("safe_point_context", sa.Text(), nullable=False),
        sa.Column("default_route", sa.String(length=255), nullable=False),
        sa.Column("mcp_session_id", sa.String(length=80), nullable=True),
        sa.Column("mcp_snapshot_id", sa.String(length=80), nullable=True),
        _json("artifact_ids"),
        sa.Column("is_user_visible", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "mcp_session_id"],
            ["mcp_sessions.owner_id", "mcp_sessions.task_id", "mcp_sessions.session_id"],
            name="fk_checkpoint_links_owner_task_mcp_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "mcp_session_id", "mcp_snapshot_id"],
            [
                "mcp_snapshots.owner_id",
                "mcp_snapshots.task_id",
                "mcp_snapshots.session_id",
                "mcp_snapshots.snapshot_id",
            ],
            name="fk_checkpoint_links_owner_task_mcp_snapshot",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(mcp_session_id IS NULL) = (mcp_snapshot_id IS NULL)",
            name=op.f("ck_checkpoint_links_mcp_reference_pair"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_checkpoint_links_owner_task_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_checkpoint_links_owner_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("savepoint_id", name=op.f("pk_checkpoint_links")),
        sa.UniqueConstraint(
            "owner_id",
            "task_id",
            "savepoint_id",
            name="uq_checkpoint_links_owner_task_savepoint",
        ),
        sa.UniqueConstraint("run_id", "checkpoint_id", name="uq_checkpoint_links_run_checkpoint"),
        sa.UniqueConstraint(
            "run_id", "idempotency_key", name="uq_checkpoint_links_run_idempotency"
        ),
    )
    op.create_index(
        "ix_checkpoint_links_owner_task_created",
        "checkpoint_links",
        ["owner_id", "task_id", "created_at"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_artifacts_owner_task_current_version",
        "artifacts",
        "artifact_versions",
        ["owner_id", "task_id", "artifact_id", "current_version_id"],
        ["owner_id", "task_id", "artifact_id", "version_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_runs_owner_task_resume_savepoint",
        "runs",
        "checkpoint_links",
        ["owner_id", "task_id", "resume_from_savepoint_id"],
        ["owner_id", "task_id", "savepoint_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_runs_owner_task_resume_savepoint", "runs", type_="foreignkey")
    op.drop_constraint("fk_artifacts_owner_task_current_version", "artifacts", type_="foreignkey")
    op.drop_table("checkpoint_links")
    op.drop_table("mcp_snapshots")
    op.drop_table("artifact_versions")
    op.drop_table("stage_plans")
    op.drop_table("run_events")
    op.drop_table("mcp_sessions")
    op.drop_table("interrupts")
    op.drop_table("feedback")
    op.drop_table("artifacts")
    op.drop_table("runs")
    op.drop_table("tasks")
    op.drop_table("users")
