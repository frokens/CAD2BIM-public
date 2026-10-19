"""SQLAlchemy metadata for the CAD2BIM control-plane database.

The models deliberately store identifiers as strings because the public domain
contracts use prefixed IDs (for example ``task_<uuid>``), not PostgreSQL UUIDs.
Raw artifact bytes and LangGraph checkpoint bodies are owned by their dedicated
stores and therefore do not appear in this schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


ID_LENGTH = 80
OWNER_ID_LENGTH = 255
IDEMPOTENCY_KEY_LENGTH = 255


def _remove_allowed_characters(column_name: str, characters: str) -> str:
    expression = column_name
    for character in characters:
        expression = f"replace({expression}, '{character}', '')"
    return expression


_RUN_INPUT_MANIFEST_DIGEST_REMAINDER = _remove_allowed_characters(
    "input_manifest_digest",
    "0123456789abcdef",
)
_ACCEPTED_TREE_FINGERPRINT_REMAINDER = _remove_allowed_characters(
    "accepted_tree_fingerprint",
    "0123456789abcdef",
)
_GEOMETRY_CONTENT_DIGEST_REMAINDER = _remove_allowed_characters(
    "content_digest",
    "0123456789abcdef",
)
_GEOMETRY_MANIFEST_DIGEST_REMAINDER = _remove_allowed_characters(
    "manifest_digest",
    "0123456789abcdef",
)
_GEOMETRY_ASSET_SHA256_REMAINDER = _remove_allowed_characters(
    "sha256",
    "0123456789abcdef",
)
_PRODUCT_FAMILY_CONTRACT_DIGEST_REMAINDER = _remove_allowed_characters(
    "contract_digest",
    "0123456789abcdef",
)
_PRODUCT_FAMILY_CONTENT_DIGEST_REMAINDER = _remove_allowed_characters(
    "content_digest",
    "0123456789abcdef",
)
_PRODUCT_FAMILY_GEOMETRY_DIGEST_REMAINDER = _remove_allowed_characters(
    "geometry_content_digest",
    "0123456789abcdef",
)
_PRODUCT_FAMILY_ASSET_SHA256_REMAINDER = _remove_allowed_characters(
    "sha256",
    "0123456789abcdef",
)
_PRODUCT_FAMILY_BINDING_KEY_REMAINDER = _remove_allowed_characters(
    "binding_key",
    "abcdefghijklmnopqrstuvwxyz0123456789_",
)
_GEOMETRY_QUERY_REQUEST_DIGEST_REMAINDER = _remove_allowed_characters(
    "request_digest",
    "0123456789abcdef",
)
_IFC_QUERY_REQUEST_DIGEST_REMAINDER = _remove_allowed_characters(
    "request_digest",
    "0123456789abcdef",
)
_IFC_QUERY_INPUT_NAME_REMAINDER = _remove_allowed_characters(
    "input_name",
    "abcdefghijklmnopqrstuvwxyz0123456789_",
)
_DATA_QUERY_REQUEST_DIGEST_REMAINDER = _remove_allowed_characters(
    "request_digest",
    "0123456789abcdef",
)


def _json_type() -> JSON:
    """Use JSONB on PostgreSQL while keeping metadata portable for tests."""

    # Nullable domain fields use SQL NULL as the absence marker.  SQLAlchemy's
    # JSON default serializes Python ``None`` as the JSON value ``null``, which
    # is not SQL NULL and therefore breaks the paired-column database checks
    # used for Artifact references.
    return JSON(none_as_null=True).with_variant(
        JSONB(none_as_null=True),
        "postgresql",
    )


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UpdatedAtMixin(CreatedAtMixin):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TenantScopedMixin:
    """Required tenant scope for every persisted owner-scoped resource."""

    tenant_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


def _tenant_task_scope_fk(table_name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "task_id"],
        ["tasks.tenant_id", "tasks.task_id"],
        name=f"fk_{table_name}_tenant_task_tasks",
        ondelete="CASCADE",
    )


class PrincipalModel(Base, UpdatedAtMixin):
    __tablename__ = "principals"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_principals_issuer_subject"),
        UniqueConstraint("legacy_owner_id", name="uq_principals_legacy_owner"),
        CheckConstraint("status IN ('active', 'disabled')", name="status_valid"),
    )

    principal_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    legacy_owner_id: Mapped[str | None] = mapped_column(String(OWNER_ID_LENGTH))


class TenantModel(Base, UpdatedAtMixin):
    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("legacy_owner_id", name="uq_tenants_legacy_owner"),
        CheckConstraint("kind = 'personal'", name="kind_valid"),
        CheckConstraint("status IN ('active', 'suspended')", name="status_valid"),
    )

    tenant_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    legacy_owner_id: Mapped[str | None] = mapped_column(String(OWNER_ID_LENGTH))


class TenantPrincipalModel(Base, CreatedAtMixin):
    __tablename__ = "tenant_principals"
    __table_args__ = (
        CheckConstraint("access = 'owner'", name="access_valid"),
        UniqueConstraint("principal_id", name="uq_tenant_principals_principal"),
    )

    tenant_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    principal_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("principals.principal_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    access: Mapped[str] = mapped_column(String(32), nullable=False)


class TaskTenantBindingModel(Base, CreatedAtMixin):
    __tablename__ = "task_tenant_bindings"
    __table_args__ = (
        _tenant_task_scope_fk("task_tenant_bindings"),
        ForeignKeyConstraint(
            ["legacy_owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_task_tenant_bindings_legacy_task",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "legacy_owner_id",
            "task_id",
            name="uq_task_tenant_bindings_legacy_task",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    principal_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("principals.principal_id", ondelete="RESTRICT"),
        nullable=False,
    )
    legacy_owner_id: Mapped[str | None] = mapped_column(String(OWNER_ID_LENGTH))


class IdentityMigrationQuarantineModel(Base, CreatedAtMixin):
    __tablename__ = "identity_migration_quarantine"

    legacy_owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), primary_key=True)
    reason_code: Mapped[str] = mapped_column(String(255), nullable=False)
    task_count: Mapped[int] = mapped_column(Integer, nullable=False)


class UserModel(Base, UpdatedAtMixin, TenantScopedMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("owner_id", "tenant_id", name="uq_users_owner_tenant"),)

    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(500))


class TaskModel(Base, UpdatedAtMixin, TenantScopedMixin):
    __tablename__ = "tasks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "tenant_id"],
            ["users.owner_id", "users.tenant_id"],
            name="fk_tasks_owner_tenant_users",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("owner_id", "task_id", name="uq_tasks_owner_task"),
        UniqueConstraint("tenant_id", "task_id", name="uq_tasks_tenant_task"),
        UniqueConstraint("owner_id", "idempotency_key", name="uq_tasks_owner_idempotency"),
        CheckConstraint(
            "(idempotency_key IS NULL) = (request_fingerprint IS NULL)",
            name="idempotency_fingerprint_pair",
        ),
        CheckConstraint(
            "status IN ('draft', 'running', 'paused', 'waiting_user', "
            "'completed', 'failed', 'cancelled', 'archived')",
            name="status_valid",
        ),
        Index("ix_tasks_owner_created", "owner_id", "created_at"),
    )

    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        String(OWNER_ID_LENGTH), ForeignKey("users.owner_id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    instruction: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    active_module: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_uri: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))


class RunModel(Base, UpdatedAtMixin, TenantScopedMixin):
    __tablename__ = "runs"
    __table_args__ = (
        _tenant_task_scope_fk("runs"),
        ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_runs_owner_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "resume_from_savepoint_id"],
            [
                "checkpoint_links.owner_id",
                "checkpoint_links.task_id",
                "checkpoint_links.savepoint_id",
            ],
            name="fk_runs_owner_task_resume_savepoint",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "final_artifact_id"],
            [
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ],
            name="fk_runs_final_artifact_scope",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "final_artifact_id", "final_artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_runs_final_artifact_version",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        UniqueConstraint("owner_id", "task_id", "run_id", name="uq_runs_owner_task_run"),
        UniqueConstraint("task_id", "idempotency_key", name="uq_runs_task_idempotency"),
        CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'waiting_user', "
            "'completed', 'failed', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint("claim_count >= 0", name="claim_count_nonnegative"),
        CheckConstraint("writer_generation >= 0", name="writer_generation_nonnegative"),
        CheckConstraint(
            "recovery_generation >= 0",
            name="recovery_generation_nonnegative",
        ),
        CheckConstraint(
            "restore_defer_count >= 0",
            name="restore_defer_count_nonnegative",
        ),
        CheckConstraint(
            "(claimed_by IS NULL) = (lease_token IS NULL)",
            name="claim_lease_token_pair",
        ),
        CheckConstraint(
            "(claimed_by IS NULL) = (lease_expires_at IS NULL)",
            name="claim_lease_expiry_pair",
        ),
        CheckConstraint(
            "(final_artifact_id IS NULL) = (final_artifact_version_id IS NULL) "
            "AND (final_artifact_id IS NULL) = (final_artifact IS NULL)",
            name="final_artifact_fields_consistent",
        ),
        CheckConstraint(
            "(execution_mode = 'graph' AND ((execution_protocol_status = 'pinned' "
            "AND execution_protocol IS NOT NULL "
            "AND execution_protocol_quarantine_reason IS NULL) "
            "OR (execution_protocol_status = 'quarantined' "
            "AND execution_protocol IS NULL "
            "AND execution_protocol_quarantine_reason IS NOT NULL))) "
            "OR (execution_mode = 'direct_mcp' "
            "AND execution_protocol_status = 'not_applicable' "
            "AND execution_protocol IS NULL "
            "AND execution_protocol_quarantine_reason IS NULL)",
            name="execution_protocol_mapping_valid",
        ),
        CheckConstraint(
            "execution_mode = 'graph' OR ("
            "status <> 'queued' AND claimed_by IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL "
            "AND claim_not_before IS NULL AND claim_count = 0)",
            name="direct_run_not_runner_claimable",
        ),
        CheckConstraint(
            "(input_manifest IS NULL) = (input_manifest_digest IS NULL)",
            name="input_manifest_pair",
        ),
        CheckConstraint(
            "input_manifest_digest IS NULL OR "
            "(length(input_manifest_digest) = 64 AND "
            f"{_RUN_INPUT_MANIFEST_DIGEST_REMAINDER} = '')",
            name="input_manifest_digest_shape",
        ),
        Index(
            "uq_runs_task_active",
            "task_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'paused', 'waiting_user')"),
            sqlite_where=text("status IN ('queued', 'running', 'paused', 'waiting_user')"),
        ),
        Index(
            "ix_runs_claim_queue",
            "execution_mode",
            "status",
            "claim_not_before",
            "lease_expires_at",
            "created_at",
        ),
        Index("ix_runs_owner_task_created", "owner_id", "task_id", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    module: Mapped[str] = mapped_column(String(64), nullable=False)
    stage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    input_manifest: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    input_manifest_digest: Mapped[str | None] = mapped_column(String(64))
    initial_feedback: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    pause_request_id: Mapped[str | None] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH))
    continue_request_id: Mapped[str | None] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH))
    idempotency_key: Mapped[str] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    execution_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'graph'")
    )
    execution_protocol_status: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_protocol: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    execution_protocol_quarantine_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    lease_token: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    writer_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    reclaimed_lease: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    claim_not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    restore_defer_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    restore_defer_reason_code: Mapped[str | None] = mapped_column(String(100))
    pause_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    feedback_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    feedback_sealed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    feedback_transition_subject_id: Mapped[str | None] = mapped_column(String(100))
    active_feedback_subject: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    resume_from_savepoint_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    error: Mapped[str | None] = mapped_column(Text)
    final_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    final_artifact_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    final_artifact: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CheckpointProjectionReceiptModel(Base, CreatedAtMixin, TenantScopedMixin):
    """Committed idempotency receipt for one exact checkpoint projector."""

    __tablename__ = "checkpoint_projection_receipts"
    __table_args__ = (
        _tenant_task_scope_fk("checkpoint_projection_receipts"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_projection_receipts_owner_task_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "run_id",
            "thread_id",
            "checkpoint_id",
            "checkpoint_ns",
            "root_checkpoint_id",
            "checkpoint_map_digest",
            "projector_name",
            "projector_version",
            name="uq_projection_receipts_exact_projector",
        ),
        CheckConstraint(
            "(first_sequence IS NULL) = (last_sequence IS NULL)",
            name="event_sequence_pair",
        ),
        CheckConstraint(
            "first_sequence IS NULL OR first_sequence <= last_sequence",
            name="event_sequence_order",
        ),
        Index(
            "ix_projection_receipts_owner_run_created",
            "owner_id",
            "run_id",
            "created_at",
        ),
    )

    receipt_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    checkpoint_id: Mapped[str] = mapped_column(String(255), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(String(1_024), nullable=False)
    root_checkpoint_id: Mapped[str] = mapped_column(String(255), nullable=False)
    checkpoint_map_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    projector_name: Mapped[str] = mapped_column(String(100), nullable=False)
    projector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    output_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    first_sequence: Mapped[int | None] = mapped_column(BigInteger)
    last_sequence: Mapped[int | None] = mapped_column(BigInteger)


class RunStageBusinessViewModel(Base, UpdatedAtMixin, TenantScopedMixin):
    """Stage-scoped Task/Check read model derived from an exact checkpoint."""

    __tablename__ = "run_stage_business_views"
    __table_args__ = (
        _tenant_task_scope_fk("run_stage_business_views"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_stage_business_views_owner_task_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "definition_revision >= 1",
            name="definition_revision_positive",
        ),
        CheckConstraint("revision >= 1", name="revision_positive"),
        Index(
            "ix_stage_business_views_owner_run_updated",
            "owner_id",
            "run_id",
            "updated_at",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    module: Mapped[str] = mapped_column(String(64), primary_key=True)
    stage_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    definition_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_plan: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    task_tree: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_checkpoint_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_checkpoint_ns: Mapped[str] = mapped_column(String(1_024), nullable=False)
    source_root_checkpoint_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_checkpoint_map_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    projector_name: Mapped[str] = mapped_column(String(100), nullable=False)
    projector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    output_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class RunArtifactViewModel(Base, UpdatedAtMixin, TenantScopedMixin):
    """Run-scoped logical Artifact version anchored to an exact checkpoint."""

    __tablename__ = "run_artifact_views"
    __table_args__ = (
        _tenant_task_scope_fk("run_artifact_views"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_run_artifact_views_owner_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "artifact_id", "artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_run_artifact_views_owner_task_artifact_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "run_id",
            "artifact_id",
            "artifact_version_id",
            name="uq_run_artifact_views_run_artifact_version",
        ),
        CheckConstraint(
            "category IN ('document', 'artifact')",
            name="category_valid",
        ),
        Index(
            "ix_run_artifact_views_owner_run_created",
            "owner_id",
            "run_id",
            "created_at",
            "resource_id",
        ),
    )

    resource_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    module: Mapped[str] = mapped_column(String(64), nullable=False)
    stage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    artifact_version_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    artifact_version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_refs: Mapped[list[str]] = mapped_column(_json_type(), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_checkpoint_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_checkpoint_ns: Mapped[str] = mapped_column(String(1_024), nullable=False)
    source_root_checkpoint_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_checkpoint_map_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_agent: Mapped[str | None] = mapped_column(String(200))
    attempt: Mapped[int | None] = mapped_column(Integer)
    projector_name: Mapped[str] = mapped_column(String(100), nullable=False)
    projector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    output_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class RunEventModel(Base, CreatedAtMixin, TenantScopedMixin):
    __tablename__ = "run_events"
    __table_args__ = (
        _tenant_task_scope_fk("run_events"),
        ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_run_events_owner_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_run_events_owner_task_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
        UniqueConstraint("run_id", "source_event_id", name="uq_run_events_run_source"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint(
            "audience IN ('public', 'operator', 'internal')",
            name="audience_valid",
        ),
        Index("ix_run_events_owner_run_sequence", "owner_id", "run_id", "sequence"),
        Index("ix_run_events_run_item", "run_id", "item_id"),
    )

    event_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    source_event_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    audience: Mapped[str] = mapped_column(String(16), nullable=False)
    item_id: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_item_id: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    subject_refs: Mapped[list[str]] = mapped_column(_json_type(), nullable=False)
    metrics: Mapped[dict[str, int | float]] = mapped_column(_json_type(), nullable=False)
    attributes: Mapped[dict[str, str]] = mapped_column(_json_type(), nullable=False)
    payload_ref: Mapped[str | None] = mapped_column(Text)


class InterruptModel(Base, CreatedAtMixin, TenantScopedMixin):
    __tablename__ = "interrupts"
    __table_args__ = (
        _tenant_task_scope_fk("interrupts"),
        ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_interrupts_owner_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_interrupts_owner_task_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint("run_id", "idempotency_key", name="uq_interrupts_run_idempotency"),
        UniqueConstraint(
            "run_id",
            "response_idempotency_key",
            name="uq_interrupts_run_response_idempotency",
        ),
        CheckConstraint("status IN ('pending', 'responded')", name="status_valid"),
        Index(
            "uq_interrupts_run_pending",
            "run_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index("ix_interrupts_owner_run_status", "owner_id", "run_id", "status"),
    )

    interrupt_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    response: Mapped[Any | None] = mapped_column(_json_type())
    idempotency_key: Mapped[str] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_idempotency_key: Mapped[str | None] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FeedbackCommandModel(Base, CreatedAtMixin, TenantScopedMixin):
    __tablename__ = "feedback_commands"
    __table_args__ = (
        _tenant_task_scope_fk("feedback_commands"),
        ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_feedback_commands_owner_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_feedback_commands_owner_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "author_principal_id"],
            ["tenant_principals.tenant_id", "tenant_principals.principal_id"],
            name="fk_feedback_commands_tenant_author",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "author_principal_id",
            "client_request_id",
            name="uq_feedback_commands_idempotency",
        ),
        UniqueConstraint(
            "run_id",
            "accepted_sequence",
            name="uq_feedback_commands_run_sequence",
        ),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "feedback_id",
            name="uq_feedback_commands_tenant_run_feedback",
        ),
        CheckConstraint("accepted_sequence > 0", name="accepted_sequence_positive"),
        CheckConstraint(
            "delivery_mode IN ('steer', 'interrupt')",
            name="delivery_mode_valid",
        ),
        CheckConstraint(
            "status IN ('accepted', 'claimed', 'delivered', 'superseded')",
            name="status_valid",
        ),
        CheckConstraint(
            "((status = 'accepted' AND claimed_batch_id IS NULL "
            "AND delivered_checkpoint_id IS NULL AND delivered_at IS NULL) OR "
            "(status = 'claimed' AND claimed_batch_id IS NOT NULL "
            "AND delivered_checkpoint_id IS NULL AND delivered_at IS NULL) OR "
            "(status = 'delivered' AND claimed_batch_id IS NOT NULL "
            "AND delivered_checkpoint_id IS NOT NULL AND delivered_at IS NOT NULL) OR "
            "(status = 'superseded' AND claimed_batch_id IS NULL "
            "AND delivered_checkpoint_id IS NULL AND delivered_at IS NULL))",
            name="lifecycle_consistent",
        ),
        Index(
            "ix_feedback_commands_owner_run_status",
            "owner_id",
            "run_id",
            "status",
        ),
        Index(
            "ix_feedback_commands_owner_run_sequence",
            "owner_id",
            "run_id",
            "accepted_sequence",
        ),
    )

    feedback_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    accepted_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    author_principal_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    resolved_scope: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    content: Mapped[list[dict[str, Any]]] = mapped_column(_json_type(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    claimed_batch_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    delivered_checkpoint_id: Mapped[str | None] = mapped_column(String(255))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FeedbackDeliveryBatchModel(Base, CreatedAtMixin, TenantScopedMixin):
    __tablename__ = "feedback_delivery_batches"
    __table_args__ = (
        _tenant_task_scope_fk("feedback_delivery_batches"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_feedback_delivery_batches_owner_task_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "batch_id",
            name="uq_feedback_delivery_batches_tenant_run_batch",
        ),
        UniqueConstraint(
            "run_id",
            "target_fingerprint",
            "claim_high_watermark",
            "claimed_writer_generation",
            name="uq_feedback_delivery_batches_replay",
        ),
        UniqueConstraint(
            "resume_interrupt_id",
            name="uq_feedback_delivery_batches_resume_interrupt",
        ),
        CheckConstraint("claim_high_watermark > 0", name="watermark_positive"),
        CheckConstraint(
            "claimed_writer_generation > 0",
            name="writer_generation_positive",
        ),
        CheckConstraint(
            "(delivered_checkpoint_id IS NULL) = (delivered_at IS NULL)",
            name="delivered_fields_consistent",
        ),
        Index(
            "ix_feedback_delivery_batches_owner_run_created",
            "owner_id",
            "run_id",
            "created_at",
        ),
    )

    batch_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    target_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_high_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False)
    contains_interrupt: Mapped[bool] = mapped_column(Boolean, nullable=False)
    claimed_writer_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    claimed_checkpoint_id: Mapped[str] = mapped_column(String(255), nullable=False)
    claimed_checkpoint_cursor: Mapped[dict[str, Any]] = mapped_column(
        _json_type(), nullable=False
    )
    resume_interrupt_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey(
            "interrupts.interrupt_id",
            name="fk_feedback_delivery_batches_resume_interrupt",
            ondelete="CASCADE",
        ),
    )
    delivered_checkpoint_id: Mapped[str | None] = mapped_column(String(255))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FeedbackDeliveryBatchMemberModel(Base, TenantScopedMixin):
    __tablename__ = "feedback_delivery_batch_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "batch_id"],
            [
                "feedback_delivery_batches.tenant_id",
                "feedback_delivery_batches.run_id",
                "feedback_delivery_batches.batch_id",
            ],
            name="fk_feedback_delivery_batch_members_batch",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "feedback_id"],
            [
                "feedback_commands.tenant_id",
                "feedback_commands.run_id",
                "feedback_commands.feedback_id",
            ],
            name="fk_feedback_delivery_batch_members_command",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "batch_id",
            "accepted_sequence",
            name="uq_feedback_delivery_batch_members_sequence",
        ),
        CheckConstraint("accepted_sequence > 0", name="accepted_sequence_positive"),
    )

    batch_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    feedback_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    accepted_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)


class UserSavepointCommandModel(Base, UpdatedAtMixin, TenantScopedMixin):
    """Durable request claimed only by the fenced Runner at a real checkpoint."""

    __tablename__ = "user_savepoint_commands"
    __table_args__ = (
        _tenant_task_scope_fk("user_savepoint_commands"),
        ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_user_savepoint_commands_owner_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_user_savepoint_commands_owner_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "author_principal_id"],
            ["tenant_principals.tenant_id", "tenant_principals.principal_id"],
            name="fk_user_savepoint_commands_tenant_author",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "savepoint_id"],
            [
                "checkpoint_links.owner_id",
                "checkpoint_links.task_id",
                "checkpoint_links.savepoint_id",
            ],
            name="fk_user_savepoint_commands_linked_savepoint",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "author_principal_id",
            "client_request_id",
            name="uq_user_savepoint_commands_idempotency",
        ),
        CheckConstraint(
            "status IN ('requested', 'claimed', 'linked')",
            name="status_valid",
        ),
        CheckConstraint(
            "claimed_writer_generation IS NULL OR claimed_writer_generation > 0",
            name="writer_generation_positive",
        ),
        CheckConstraint(
            "claimed_lease_token_digest IS NULL OR (length(claimed_lease_token_digest) = 64)",
            name="lease_digest_shape",
        ),
        CheckConstraint(
            "((status = 'requested' "
            "AND claimed_runner_id IS NULL "
            "AND claimed_lease_token_digest IS NULL "
            "AND claimed_writer_generation IS NULL "
            "AND claimed_stage_key IS NULL "
            "AND claimed_checkpoint_id IS NULL "
            "AND claimed_checkpoint_ns IS NULL "
            "AND claimed_at IS NULL "
            "AND savepoint_id IS NULL "
            "AND linked_at IS NULL) OR "
            "(status = 'claimed' "
            "AND claimed_runner_id IS NOT NULL "
            "AND claimed_lease_token_digest IS NOT NULL "
            "AND claimed_writer_generation IS NOT NULL "
            "AND claimed_stage_key IS NOT NULL "
            "AND claimed_checkpoint_id IS NOT NULL "
            "AND claimed_checkpoint_ns IS NOT NULL "
            "AND claimed_at IS NOT NULL "
            "AND savepoint_id IS NULL "
            "AND linked_at IS NULL) OR "
            "(status = 'linked' "
            "AND claimed_runner_id IS NOT NULL "
            "AND claimed_lease_token_digest IS NOT NULL "
            "AND claimed_writer_generation IS NOT NULL "
            "AND claimed_stage_key IS NOT NULL "
            "AND claimed_checkpoint_id IS NOT NULL "
            "AND claimed_checkpoint_ns IS NOT NULL "
            "AND claimed_at IS NOT NULL "
            "AND savepoint_id IS NOT NULL "
            "AND linked_at IS NOT NULL))",
            name="lifecycle_consistent",
        ),
        Index(
            "ix_user_savepoint_commands_owner_run_status",
            "owner_id",
            "run_id",
            "status",
            "created_at",
        ),
    )

    command_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    author_principal_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    claimed_runner_id: Mapped[str | None] = mapped_column(String(255))
    claimed_lease_token_digest: Mapped[str | None] = mapped_column(String(64))
    claimed_writer_generation: Mapped[int | None] = mapped_column(BigInteger)
    claimed_stage_key: Mapped[str | None] = mapped_column(String(255))
    claimed_checkpoint_id: Mapped[str | None] = mapped_column(String(255))
    claimed_checkpoint_ns: Mapped[str | None] = mapped_column(String(1_024))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    savepoint_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StagePlanModel(Base, CreatedAtMixin, TenantScopedMixin):
    __tablename__ = "stage_plans"
    __table_args__ = (
        _tenant_task_scope_fk("stage_plans"),
        ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_stage_plans_owner_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_stage_plans_owner_task_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "run_id",
            "stage_key",
            "definition_revision",
            name="uq_stage_plans_run_stage_definition_revision",
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "stage_key",
            "definition_revision",
            "plan_id",
            name="uq_stage_plans_projection_scope",
        ),
        CheckConstraint(
            "definition_revision > 0",
            name="definition_revision_positive",
        ),
    )

    stage_plan_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    definition_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)


class ArtifactModel(Base, UpdatedAtMixin, TenantScopedMixin):
    __tablename__ = "artifacts"
    __table_args__ = (
        _tenant_task_scope_fk("artifacts"),
        ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_artifacts_owner_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_artifacts_owner_task_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "artifact_id", "current_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_artifacts_owner_task_current_version",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "owner_id", "task_id", "artifact_id", name="uq_artifacts_owner_task_artifact"
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "artifact_id",
            name="uq_artifacts_owner_task_run_artifact",
        ),
        UniqueConstraint("task_id", "idempotency_key", name="uq_artifacts_task_idempotency"),
        CheckConstraint(
            "status IN ('created', 'uploading', 'ready', 'available', "
            "'failed', 'archived', 'deleted')",
            name="status_valid",
        ),
        Index("ix_artifacts_owner_task_created", "owner_id", "task_id", "created_at"),
    )

    artifact_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    idempotency_key: Mapped[str] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class ArtifactVersionModel(Base, CreatedAtMixin, TenantScopedMixin):
    __tablename__ = "artifact_versions"
    __table_args__ = (
        _tenant_task_scope_fk("artifact_versions"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "artifact_id"],
            ["artifacts.owner_id", "artifacts.task_id", "artifacts.artifact_id"],
            name="fk_artifact_versions_owner_task_artifact",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "owner_id", "task_id", "version_id", name="uq_artifact_versions_owner_task_version"
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "artifact_id",
            "version_id",
            name="uq_artifact_versions_owner_task_artifact_version_id",
        ),
        UniqueConstraint("artifact_id", "version_no", name="uq_artifact_versions_artifact_version"),
        CheckConstraint("version_no > 0", name="version_positive"),
        CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
    )

    version_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    lineage_json: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)


class DirectSessionBindingModel(Base, CreatedAtMixin, TenantScopedMixin):
    """Configured local subject and name bound to one persistent Task/Run."""

    __tablename__ = "direct_session_bindings"
    __table_args__ = (
        _tenant_task_scope_fk("direct_session_bindings"),
        ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_direct_session_bindings_owner_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_direct_session_bindings_owner_task_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "configured_subject",
            "normalized_session_name",
            name="uq_direct_session_bindings_subject_name",
        ),
        UniqueConstraint(
            "tenant_id",
            "configured_subject",
            "direct_session_alias",
            name="uq_direct_session_bindings_subject_alias",
        ),
        UniqueConstraint(
            "tenant_id",
            "configured_subject",
            "ordinal",
            name="uq_direct_session_bindings_subject_ordinal",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["tenant_principals.tenant_id", "tenant_principals.principal_id"],
            name="fk_direct_session_bindings_tenant_principal",
            ondelete="RESTRICT",
        ),
    )

    session_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    direct_session_alias: Mapped[str] = mapped_column(String(32), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    configured_subject: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    session_name: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_session_name: Mapped[str] = mapped_column(String(100), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)


class DataPackageModel(Base, CreatedAtMixin, TenantScopedMixin):
    __tablename__ = "data_packages"
    __table_args__ = (
        _tenant_task_scope_fk("data_packages"),
        ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_data_packages_owner_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "created_by_run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_data_packages_owner_task_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "owner_id", "task_id", "package_id", name="uq_data_packages_owner_task_package"
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "package_id",
            "request_fingerprint",
            name="uq_data_packages_scope_fingerprint",
        ),
        UniqueConstraint("task_id", "idempotency_key", name="uq_data_packages_task_idempotency"),
        CheckConstraint("package_kind IN ('source_l0', 'derived')", name="package_kind_valid"),
        CheckConstraint(
            "(package_kind = 'source_l0' AND source_filename IS NOT NULL) OR "
            "(package_kind = 'derived' AND source_filename IS NULL)",
            name="source_filename_kind_valid",
        ),
        Index("ix_data_packages_task_created", "task_id", "created_at"),
    )

    package_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    created_by_run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    package_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_filename: Mapped[str | None] = mapped_column(String(255))
    description_title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    section_titles_json: Mapped[list[str]] = mapped_column(
        _json_type(), nullable=False, default=list
    )
    idempotency_key: Mapped[str] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class DataPackageMemberModel(Base, CreatedAtMixin, TenantScopedMixin):
    __tablename__ = "data_package_members"
    __table_args__ = (
        _tenant_task_scope_fk("data_package_members"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "package_id"],
            ["data_packages.owner_id", "data_packages.task_id", "data_packages.package_id"],
            name="fk_data_package_members_owner_task_package",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "artifact_id", "artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_data_package_members_exact_artifact_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("package_id", "member_name", name="uq_data_package_members_member_name"),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "package_id",
            "member_id",
            name="uq_data_package_members_scope_member",
        ),
        CheckConstraint(
            "member_kind IN ('source', 'manifest', 'payload', 'description', 'code', 'schema')",
            name="member_kind_valid",
        ),
        CheckConstraint(
            "json_kind IS NULL OR json_kind IN ('document', 'records')",
            name="json_kind_valid",
        ),
        CheckConstraint("row_count IS NULL OR row_count >= 0", name="row_count_nonnegative"),
        CheckConstraint(
            "member_kind = 'payload' OR row_count IS NULL",
            name="row_count_payload_only",
        ),
        CheckConstraint(
            "(member_name = 'manifest.json' AND member_kind = 'manifest' "
            "AND media_type = 'application/json' AND row_count IS NULL) OR "
            "(member_name = 'source/original.dxf' AND member_kind = 'source' "
            "AND media_type = 'application/dxf' AND row_count IS NULL) OR "
            "(member_name = 'description.md' AND member_kind = 'description' "
            "AND media_type = 'text/markdown' AND row_count IS NULL) OR "
            "(member_name = 'code/transformation.sql' AND member_kind = 'code' "
            "AND media_type = 'application/sql' AND row_count IS NULL) OR "
            "(member_name = 'code/transformation.py' AND member_kind = 'code' "
            "AND media_type = 'text/x-python' AND row_count IS NULL) OR "
            "(member_kind = 'payload' AND media_type = 'application/vnd.apache.parquet' "
            "AND row_count IS NOT NULL AND json_kind IS NULL) OR "
            "(member_kind = 'payload' AND media_type = 'application/json' "
            "AND member_name LIKE 'payloads/%.json' AND row_count IS NULL "
            "AND json_kind IN ('document', 'records')) OR "
            "(member_kind = 'schema' AND media_type = 'application/schema+json' "
            "AND member_name LIKE 'schemas/%.schema.json' AND row_count IS NULL)",
            name="member_contract_valid",
        ),
        CheckConstraint(
            "member_kind = 'payload' OR (logical_payload_name IS NULL "
            "AND payload_description IS NULL AND logical_key_fields_json IS NULL "
            "AND arrow_schema_json IS NULL AND logical_byte_count IS NULL "
            "AND json_kind IS NULL)",
            name="payload_metadata_scope_valid",
        ),
        CheckConstraint(
            "member_kind <> 'payload' OR "
            "(media_type = 'application/vnd.apache.parquet' AND row_count IS NOT NULL "
            "AND json_kind IS NULL) OR "
            "(media_type = 'application/json' AND row_count IS NULL "
            "AND json_kind IN ('document', 'records') "
            "AND logical_key_fields_json IS NULL AND arrow_schema_json IS NULL)",
            name="payload_format_metadata_valid",
        ),
        CheckConstraint(
            "logical_byte_count IS NULL OR logical_byte_count >= 0",
            name="logical_byte_count_nonnegative",
        ),
    )

    member_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    package_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    member_name: Mapped[str] = mapped_column(String(255), nullable=False)
    member_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    artifact_version_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    row_count: Mapped[int | None] = mapped_column(BigInteger)
    logical_payload_name: Mapped[str | None] = mapped_column(String(120))
    payload_description: Mapped[str | None] = mapped_column(String(500))
    logical_key_fields_json: Mapped[list[str] | None] = mapped_column(_json_type())
    arrow_schema_json: Mapped[list[dict[str, Any]] | None] = mapped_column(_json_type())
    logical_byte_count: Mapped[int | None] = mapped_column(BigInteger)
    json_kind: Mapped[str | None] = mapped_column(String(16))


class CriticalOperationModel(Base, CreatedAtMixin, TenantScopedMixin):
    __tablename__ = "critical_operations"
    __table_args__ = (
        _tenant_task_scope_fk("critical_operations"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_critical_operations_owner_task_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "code_artifact_id", "code_artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_critical_operations_exact_code_artifact_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "inspection_request_artifact_id", "inspection_request_artifact_version_id"],
            ["artifact_versions.owner_id", "artifact_versions.task_id", "artifact_versions.artifact_id", "artifact_versions.version_id"],
            name="fk_critical_operations_inspection_request_artifact", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "inspection_result_artifact_id", "inspection_result_artifact_version_id"],
            ["artifact_versions.owner_id", "artifact_versions.task_id", "artifact_versions.artifact_id", "artifact_versions.version_id"],
            name="fk_critical_operations_inspection_result_artifact", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["inspection_source_geometry_version_id"],
            ["geometry_resource_versions.resource_version_id"],
            name="fk_critical_operations_inspection_geometry_source", ondelete="RESTRICT", use_alter=True,
        ),
        ForeignKeyConstraint(
            ["inspection_source_family_version_id"],
            ["product_family_versions.family_version_id"],
            name="fk_critical_operations_inspection_family_source", ondelete="RESTRICT", use_alter=True,
        ),
        ForeignKeyConstraint(
            [
                "owner_id",
                "task_id",
                "description_artifact_id",
                "description_artifact_version_id",
            ],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_critical_operations_exact_description_artifact_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "repair_parent_operation_id"],
            [
                "critical_operations.owner_id",
                "critical_operations.task_id",
                "critical_operations.operation_id",
            ],
            name="fk_critical_operations_repair_parent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "result_package_id", "request_fingerprint"],
            [
                "data_packages.owner_id",
                "data_packages.task_id",
                "data_packages.package_id",
                "data_packages.request_fingerprint",
            ],
            name="fk_critical_operations_result_package",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "source_ifc_model_id", "source_ifc_revision_id"],
            ["ifc_model_revisions.owner_id", "ifc_model_revisions.task_id",
             "ifc_model_revisions.run_id", "ifc_model_revisions.ifc_model_id",
             "ifc_model_revisions.revision_id"],
            name="fk_critical_operations_ifc_read_source",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(source_ifc_model_id IS NULL AND source_ifc_revision_id IS NULL) OR "
            "(source_ifc_model_id IS NOT NULL AND source_ifc_revision_id IS NOT NULL "
            "AND operation_kind = 'data_transform' AND tool_name = 'data_derive' "
            "AND COALESCE(operation_detail_json ->> 'detail_kind', '') = 'data_transform' "
            "AND COALESCE(operation_detail_json ->> 'engine', '') = 'python_arrow')",
            name="ifc_read_source_consistent",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "base_ifc_revision_id"],
            [
                "ifc_model_revisions.owner_id",
                "ifc_model_revisions.task_id",
                "ifc_model_revisions.run_id",
                "ifc_model_revisions.revision_id",
            ],
            name="fk_critical_operations_base_ifc_revision",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_geometry_resource_id", "result_geometry_resource_version_id"],
            [
                "geometry_resource_versions.resource_id",
                "geometry_resource_versions.resource_version_id",
            ],
            name="fk_critical_operations_exact_geometry_result_version",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_product_family_id", "base_product_family_version_id"],
            [
                "product_family_versions.family_id",
                "product_family_versions.family_version_id",
            ],
            name="fk_critical_operations_exact_product_family_base_version",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_product_family_id", "result_product_family_version_id"],
            [
                "product_family_versions.family_id",
                "product_family_versions.family_version_id",
            ],
            name="fk_critical_operations_exact_product_family_result_version",
            use_alter=True,
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "owner_id", "task_id", "operation_id", name="uq_critical_operations_scope"
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "operation_id",
            name="uq_critical_operations_owner_task_run_operation",
        ),
        UniqueConstraint(
            "run_id", "idempotency_key", name="uq_critical_operations_run_idempotency"
        ),
        Index(
            "uq_critical_operations_run_tool_fingerprint",
            "run_id",
            "tool_name",
            "request_fingerprint",
            unique=True,
            postgresql_where=text(
                "operation_kind IN ('source_ingestion', 'data_transform', "
                "'geometry_resource_write', 'product_family_write')"
            ),
            sqlite_where=text(
                "operation_kind IN ('source_ingestion', 'data_transform', "
                "'geometry_resource_write', 'product_family_write')"
            ),
        ),
        Index(
            "uq_critical_operations_transform_request_identity",
            "run_id",
            "transform_request_identity",
            unique=True,
            postgresql_where=text("transform_request_identity IS NOT NULL"),
            sqlite_where=text("transform_request_identity IS NOT NULL"),
        ),
        CheckConstraint(
            "((transform_request_identity IS NULL AND transform_request_digest IS NULL "
            "AND transform_request_json IS NULL AND transform_resolved_json IS NULL "
            "AND transform_deadline_at IS NULL AND transform_execution_owner IS NULL "
            "AND transform_execution_generation = 0 "
            "AND transform_execution_lease_expires_at IS NULL "
            "AND transform_execution_token_sha256 IS NULL AND transform_candidate_json IS NULL) "
            "OR (tool_name IN ('data_transform', 'data_repair_transform', 'data_derive') "
            "AND transform_request_identity IS NOT NULL "
            "AND transform_request_digest IS NOT NULL "
            "AND length(transform_request_digest) = 64 "
            "AND transform_request_json IS NOT NULL AND transform_resolved_json IS NOT NULL "
            "AND transform_deadline_at IS NOT NULL AND transform_execution_generation >= 0 "
            "AND ((transform_execution_owner IS NULL "
            "AND transform_execution_lease_expires_at IS NULL "
            "AND transform_execution_token_sha256 IS NULL) "
            "OR (transform_execution_owner IS NOT NULL "
            "AND transform_execution_generation > 0 "
            "AND transform_execution_lease_expires_at IS NOT NULL "
            "AND transform_execution_token_sha256 IS NOT NULL "
            "AND length(transform_execution_token_sha256) = 64))))",
            name="transform_control_consistent",
        ),
        CheckConstraint(
            "((operation_kind NOT IN ('geometry_resource_write', 'product_family_write', 'geometry_inspection', 'authoring_publish') "
            "AND authoring_execution_owner IS NULL AND authoring_execution_generation = 0 "
            "AND authoring_execution_token_sha256 IS NULL) "
            "OR (operation_kind IN ('geometry_resource_write', 'product_family_write', 'geometry_inspection', 'authoring_publish') "
            "AND authoring_execution_generation >= 0 "
            "AND ((authoring_execution_owner IS NULL "
            "AND authoring_execution_token_sha256 IS NULL) "
            "OR (authoring_execution_owner IS NOT NULL "
            "AND authoring_execution_token_sha256 IS NOT NULL "
            "AND length(authoring_execution_token_sha256) = 64 "
            "AND authoring_execution_generation > 0))))",
            name="authoring_control_consistent",
        ),
        CheckConstraint(
            "((operation_kind <> 'ifc_model_write' AND ifc_deadline_at IS NULL "
            "AND ifc_execution_owner IS NULL AND ifc_execution_generation = 0 "
            "AND ifc_execution_token_sha256 IS NULL AND ifc_guard_pid IS NULL "
            "AND ifc_guard_birth_us IS NULL) OR (operation_kind = 'ifc_model_write' "
            "AND ifc_deadline_at IS NOT NULL AND ifc_execution_generation >= 0 "
            "AND ((ifc_execution_owner IS NULL "
            "AND ifc_execution_token_sha256 IS NULL AND ifc_guard_pid IS NULL "
            "AND ifc_guard_birth_us IS NULL) OR (ifc_execution_owner IS NOT NULL "
            "AND ifc_execution_generation > 0 "
            "AND ifc_execution_token_sha256 IS NOT NULL "
            "AND length(ifc_execution_token_sha256) = 64 "
            "AND ((ifc_guard_pid IS NULL AND ifc_guard_birth_us IS NULL) "
            "OR (ifc_guard_pid IS NOT NULL AND ifc_guard_birth_us IS NOT NULL "
            "AND ifc_guard_pid > 0 AND ifc_guard_birth_us > 0))))))",
            name="ifc_execution_control_consistent",
        ),
        CheckConstraint(
            "operation_kind = 'ifc_model_write' OR ifc_candidate_json IS NULL",
            name="ifc_candidate_only_on_ifc_write",
        ),
        CheckConstraint(
            "tool_name <> 'ifc_trusted_baseline_load' OR COALESCE("
            "(operation_kind = 'ifc_model_write' "
            "AND operation_detail_json ->> 'detail_kind' = 'ifc_trusted_baseline_load' "
            "AND jsonb_typeof(operation_detail_json -> 'source_asset') = 'object' "
            "AND length(operation_detail_json -> 'source_asset' ->> 'artifact_id') > 0 "
            "AND length(operation_detail_json -> 'source_asset' ->> 'version_id') > 0 "
            "AND (operation_detail_json -> 'source_asset' ->> 'sha256') ~ '^[0-9a-f]{64}$' "
            "AND ((status = 'succeeded' AND operation_detail_json ->> 'result' = 'baseline_loaded') "
            "OR (status <> 'succeeded' AND NOT (operation_detail_json ? 'result')))), false)",
            name="ifc_trusted_baseline_detail_valid",
        ),
        UniqueConstraint("result_package_id", name="uq_critical_operations_result_package"),
        UniqueConstraint("inspection_result_artifact_version_id", name="uq_critical_operations_inspection_result"),
        CheckConstraint(
            "status IN ('prepared', 'succeeded', 'failed', 'indeterminate')",
            name="status_valid",
        ),
        CheckConstraint(
            "operation_kind IN ('source_ingestion', 'data_transform', 'ifc_model_write', "
            "'geometry_resource_write', 'product_family_write', 'geometry_inspection', 'authoring_publish')",
            name="operation_kind_valid",
        ),
        CheckConstraint(
            "initiator IN ('direct_client', 'runner')",
            name="initiator_valid",
        ),
        CheckConstraint(
            "(operation_kind = 'source_ingestion' AND tool_name = 'cad_import_source' "
            "AND source_filename IS NOT NULL AND intent_summary IS NOT NULL "
            "AND code_artifact_id IS NULL "
            "AND code_artifact_version_id IS NULL AND code_artifact_sha256 IS NULL "
            "AND description_artifact_id IS NULL "
            "AND description_artifact_version_id IS NULL "
            "AND description_artifact_sha256 IS NULL "
            "AND repair_parent_operation_id IS NULL "
            "AND base_ifc_revision_id IS NULL "
            "AND expected_ifc_write_epoch IS NULL "
            "AND target_geometry_resource_id IS NULL "
            "AND result_geometry_resource_version_id IS NULL "
            "AND target_product_family_id IS NULL "
            "AND base_product_family_version_id IS NULL "
            "AND result_product_family_version_id IS NULL) OR "
            "(operation_kind = 'data_transform' "
            "AND tool_name IN ('data_transform', 'data_repair_transform', 'data_derive', "
            "'cad_compute_measurements') "
            "AND source_filename IS NULL AND intent_summary IS NOT NULL "
            "AND code_artifact_id IS NOT NULL AND code_artifact_version_id IS NOT NULL "
            "AND code_artifact_sha256 IS NOT NULL "
            "AND ((tool_name = 'cad_compute_measurements' "
            "AND repair_parent_operation_id IS NULL) "
            "OR (tool_name = 'data_repair_transform' "
            "AND description_artifact_id IS NOT NULL "
            "AND description_artifact_version_id IS NOT NULL "
            "AND description_artifact_sha256 IS NOT NULL "
            "AND repair_parent_operation_id IS NOT NULL) "
            "OR (tool_name IN ('data_transform', 'data_derive') "
            "AND (repair_parent_operation_id IS NULL OR "
            "(repair_parent_operation_id IS NOT NULL "
            "AND description_artifact_id IS NOT NULL "
            "AND description_artifact_version_id IS NOT NULL "
            "AND description_artifact_sha256 IS NOT NULL)))) "
            "AND base_ifc_revision_id IS NULL "
            "AND expected_ifc_write_epoch IS NULL "
            "AND target_geometry_resource_id IS NULL "
            "AND result_geometry_resource_version_id IS NULL "
            "AND target_product_family_id IS NULL "
            "AND base_product_family_version_id IS NULL "
            "AND result_product_family_version_id IS NULL) OR "
            "(operation_kind = 'ifc_model_write' "
            "AND source_filename IS NULL AND intent_summary IS NOT NULL "
            "AND code_artifact_id IS NULL "
            "AND code_artifact_version_id IS NULL AND code_artifact_sha256 IS NULL "
            "AND description_artifact_id IS NULL "
            "AND description_artifact_version_id IS NULL "
            "AND description_artifact_sha256 IS NULL "
            "AND repair_parent_operation_id IS NULL "
            "AND (result_package_id IS NULL OR tool_name IN ('ifc_products_apply','ifc_products_edit')) "
            "AND target_geometry_resource_id IS NULL "
            "AND result_geometry_resource_version_id IS NULL "
            "AND target_product_family_id IS NULL "
            "AND base_product_family_version_id IS NULL "
            "AND result_product_family_version_id IS NULL "
            "AND ((tool_name = 'ifc_model_initialize' "
            "AND base_ifc_revision_id IS NULL "
            "AND expected_ifc_write_epoch IS NULL) "
            "OR (tool_name IN ('ifc_model_configure_geometry_basis', "
            "'ifc_project_update', 'ifc_spatial_structure_apply', 'ifc_products_apply', "
            "'ifc_trusted_baseline_load', 'ifc_products_edit') "
            "AND base_ifc_revision_id IS NOT NULL "
            "AND expected_ifc_write_epoch IS NOT NULL))) OR "
            "(operation_kind = 'geometry_resource_write' "
            "AND tool_name IN ('ifc_geometry_resource_create', "
            "'ifc_geometry_resource_repair', 'ifc_geometry_resource_write') "
            "AND source_filename IS NULL AND intent_summary IS NOT NULL "
            "AND description_artifact_id IS NULL "
            "AND description_artifact_version_id IS NULL "
            "AND description_artifact_sha256 IS NULL "
            "AND COALESCE(((code_artifact_id IS NOT NULL "
            "AND code_artifact_version_id IS NOT NULL "
            "AND code_artifact_sha256 IS NOT NULL "
            "AND operation_detail_json ->> 'detail_kind' IS DISTINCT FROM "
            "'geometry_interface_annotation' "
            "AND ((tool_name = 'ifc_geometry_resource_create' "
            "AND repair_parent_operation_id IS NULL) OR "
            "(tool_name = 'ifc_geometry_resource_repair' "
            "AND repair_parent_operation_id IS NOT NULL) OR "
            "tool_name = 'ifc_geometry_resource_write')) OR "
            "(tool_name = 'ifc_geometry_resource_write' "
            "AND code_artifact_id IS NULL "
            "AND code_artifact_version_id IS NULL "
            "AND code_artifact_sha256 IS NULL "
            "AND repair_parent_operation_id IS NULL "
            "AND operation_detail_json ->> 'detail_kind' = "
            "'geometry_interface_annotation' "
            "AND operation_detail_json ->> 'resource_kind' = 'mapped_model' "
            "AND operation_detail_json ->> 'target_mode' IN "
            "('same_task_root', 'derive_task_root') "
            "AND jsonb_typeof(operation_detail_json -> 'interfaces') = 'array' "
            "AND jsonb_array_length(operation_detail_json -> 'interfaces') <= 64 "
            "AND length(operation_detail_json ->> 'base_resource_id') BETWEEN 1 AND 80 "
            "AND length(operation_detail_json ->> 'base_resource_version_id') BETWEEN 1 AND 80 "
            "AND operation_detail_json ->> 'base_content_digest' ~ '^[0-9a-f]{64}$' "
            "AND length(operation_detail_json ->> 'display_name') BETWEEN 1 AND 255 "
            "AND operation_detail_json ->> 'display_name' = "
            "trim(operation_detail_json ->> 'display_name') "
            "AND ((operation_detail_json ->> 'target_mode' = 'same_task_root' "
            "AND target_geometry_resource_id = "
            "operation_detail_json ->> 'base_resource_id') OR "
            "(operation_detail_json ->> 'target_mode' = 'derive_task_root' "
            "AND target_geometry_resource_id <> "
            "operation_detail_json ->> 'base_resource_id')))), FALSE) "
            "AND result_package_id IS NULL "
            "AND base_ifc_revision_id IS NULL "
            "AND expected_ifc_write_epoch IS NULL "
            "AND target_geometry_resource_id IS NOT NULL "
            "AND target_product_family_id IS NULL "
            "AND base_product_family_version_id IS NULL "
            "AND result_product_family_version_id IS NULL) OR "
            "(COALESCE((operation_kind = 'product_family_write' "
            "AND tool_name = 'ifc_product_family_write' "
            "AND source_filename IS NULL AND intent_summary IS NOT NULL "
            "AND code_artifact_id IS NOT NULL "
            "AND code_artifact_version_id IS NOT NULL "
            "AND code_artifact_sha256 IS NOT NULL "
            "AND description_artifact_id IS NULL "
            "AND description_artifact_version_id IS NULL "
            "AND description_artifact_sha256 IS NULL "
            "AND result_package_id IS NULL "
            "AND base_ifc_revision_id IS NULL "
            "AND expected_ifc_write_epoch IS NULL "
            "AND target_geometry_resource_id IS NULL "
            "AND result_geometry_resource_version_id IS NULL "
            "AND target_product_family_id IS NOT NULL "
            "AND operation_detail_json -> 'version_draft' IS NOT NULL "
            "AND jsonb_typeof(operation_detail_json -> 'version_draft') = 'object' "
            "AND operation_detail_json ->> 'detail_kind' = 'product_family_write' "
            "AND operation_detail_json ->> 'mode' IN "
            "('create', 'repair_attempt', 'revise_version', 'system_copy') "
            "AND ((operation_detail_json ->> 'mode' IN ('create', 'system_copy') "
            "AND repair_parent_operation_id IS NULL "
            "AND base_product_family_version_id IS NULL) OR "
            "(operation_detail_json ->> 'mode' = 'repair_attempt' "
            "AND repair_parent_operation_id IS NOT NULL "
            "AND base_product_family_version_id IS NOT DISTINCT FROM "
            "operation_detail_json #>> "
            "'{version_draft,parent_family_version_id}') OR "
            "(operation_detail_json ->> 'mode' = 'revise_version' "
            "AND repair_parent_operation_id IS NULL "
            "AND base_product_family_version_id IS NOT NULL "
            "AND base_product_family_version_id = operation_detail_json #>> "
            "'{version_draft,parent_family_version_id}'))), FALSE)) OR "
            "(operation_kind = 'geometry_inspection') OR "
            "(operation_kind IN ('geometry_resource_write','product_family_write') "
            "AND operation_detail_json ->> 'detail_kind' = 'authoring_draft_write' "
            "AND code_artifact_version_id IS NOT NULL "
            "AND result_geometry_resource_version_id IS NULL AND result_product_family_version_id IS NULL "
            "AND operation_detail_json ->> 'root_id' = COALESCE(target_geometry_resource_id,target_product_family_id) "
            "AND (operation_detail_json ->> 'result_generation')::bigint = "
            "COALESCE((operation_detail_json ->> 'expected_generation')::bigint,0)+1) OR "
            "(operation_kind='authoring_publish' AND tool_name='ifc_authoring_publish' "
            "AND operation_detail_json ->> 'detail_kind'='authoring_publish' "
            "AND jsonb_typeof(operation_detail_json -> 'preview_examples')='array' "
            "AND jsonb_array_length(operation_detail_json -> 'expected_drafts')>0 "
            "AND NOT (operation_detail_json ? 'evidence') AND NOT (operation_detail_json ? 'evidence_run_id'))",
            name="operation_kind_detail_valid",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(description_artifact_id IS NULL "
            "AND description_artifact_version_id IS NULL "
            "AND description_artifact_sha256 IS NULL) OR "
            "(description_artifact_id IS NOT NULL "
            "AND description_artifact_version_id IS NOT NULL "
            "AND description_artifact_sha256 IS NOT NULL)",
            name="description_artifact_triplet_valid",
        ),
        CheckConstraint(
            "(operation_kind <> 'geometry_inspection' "
            "AND inspection_request_artifact_id IS NULL "
            "AND inspection_request_artifact_version_id IS NULL "
            "AND inspection_request_artifact_sha256 IS NULL "
            "AND inspection_result_artifact_id IS NULL "
            "AND inspection_result_artifact_version_id IS NULL "
            "AND inspection_result_artifact_sha256 IS NULL "
            "AND inspection_source_geometry_version_id IS NULL "
            "AND inspection_source_family_version_id IS NULL) OR "
            "(operation_kind = 'geometry_inspection' "
            "AND tool_name IN ('ifc_geometry_preview', 'ifc_product_realization_preview') "
            "AND source_filename IS NULL AND code_artifact_id IS NULL "
            "AND code_artifact_version_id IS NULL AND code_artifact_sha256 IS NULL "
            "AND description_artifact_id IS NULL AND description_artifact_version_id IS NULL "
            "AND description_artifact_sha256 IS NULL AND repair_parent_operation_id IS NULL "
            "AND result_package_id IS NULL AND base_ifc_revision_id IS NULL "
            "AND expected_ifc_write_epoch IS NULL AND target_geometry_resource_id IS NULL "
            "AND result_geometry_resource_version_id IS NULL AND target_product_family_id IS NULL "
            "AND base_product_family_version_id IS NULL AND result_product_family_version_id IS NULL "
            "AND inspection_request_artifact_id IS NOT NULL "
            "AND inspection_request_artifact_version_id IS NOT NULL "
            "AND inspection_request_artifact_sha256 IS NOT NULL "
            "AND (((status = 'succeeded' OR (status = 'failed' "
            "AND operation_detail_json ->> 'result_kind' = 'case_report')) AND inspection_result_artifact_id IS NOT NULL "
            "AND inspection_result_artifact_version_id IS NOT NULL "
            "AND inspection_result_artifact_sha256 IS NOT NULL) OR "
            "(status <> 'succeeded' AND inspection_result_artifact_id IS NULL "
            "AND inspection_result_artifact_version_id IS NULL "
            "AND inspection_result_artifact_sha256 IS NULL)))",
            name="inspection_artifact_bindings_valid",
        ),
        CheckConstraint(
            "operation_kind <> 'geometry_inspection' OR COALESCE(("
            "jsonb_typeof(operation_detail_json) = 'object' "
            "AND operation_detail_json ->> 'detail_kind' = 'geometry_inspection' "
            "AND operation_detail_json - ARRAY['detail_kind','source_kind','source_id',"
            "'source_version_id','source_content_digest','source_version_ref','request_artifact',"
            "'result_kind','source_type','source_draft_artifact'] = '{}'::jsonb "
            "AND length(operation_detail_json ->> 'source_id') BETWEEN 1 AND 80 "
            "AND length(operation_detail_json ->> 'source_version_id') BETWEEN 1 AND 80 "
            "AND operation_detail_json ->> 'source_content_digest' ~ '^[0-9a-f]{64}$' "
            "AND length(operation_detail_json ->> 'source_version_ref') BETWEEN 1 AND 16384 "
            "AND jsonb_typeof(operation_detail_json -> 'request_artifact') = 'object' "
            "AND (operation_detail_json -> 'request_artifact') - ARRAY['artifact_id','version_id','sha256'] = '{}'::jsonb "
            "AND operation_detail_json #>> '{request_artifact,artifact_id}' = inspection_request_artifact_id "
            "AND operation_detail_json #>> '{request_artifact,version_id}' = inspection_request_artifact_version_id "
            "AND operation_detail_json #>> '{request_artifact,sha256}' = inspection_request_artifact_sha256 "
            "AND ((operation_detail_json ->> 'source_type' = 'draft' "
            "AND inspection_source_geometry_version_id IS NULL AND inspection_source_family_version_id IS NULL "
            "AND operation_detail_json ->> 'source_version_id' = operation_detail_json #>> '{source_draft_artifact,version_id}') OR "
            "(operation_detail_json ->> 'source_kind' = 'geometry_sample' "
            "AND tool_name = 'ifc_geometry_preview' AND inspection_source_family_version_id IS NULL "
            "AND inspection_source_geometry_version_id = operation_detail_json ->> 'source_version_id') OR "
            "(operation_detail_json ->> 'source_kind' = 'family_realization' "
            "AND tool_name = 'ifc_product_realization_preview' AND inspection_source_geometry_version_id IS NULL "
            "AND inspection_source_family_version_id = operation_detail_json ->> 'source_version_id'))"
            "), FALSE)",
            name="inspection_detail_valid",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "repair_parent_operation_id IS NULL OR repair_parent_operation_id <> operation_id",
            name="repair_parent_not_self",
        ),
        CheckConstraint(
            "(status = 'prepared' AND finished_at IS NULL "
            "AND result_package_id IS NULL AND error_code IS NULL "
            "AND error_message IS NULL AND error_phase IS NULL "
            "AND result_geometry_resource_version_id IS NULL "
            "AND result_product_family_version_id IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL "
            "AND error_code IS NULL AND error_message IS NULL AND error_phase IS NULL "
            "AND ((operation_kind IN ('source_ingestion', 'data_transform') "
            "AND result_package_id IS NOT NULL "
            "AND result_geometry_resource_version_id IS NULL) OR "
            "(operation_kind = 'ifc_model_write' "
            "AND ((tool_name NOT IN ('ifc_products_apply','ifc_products_edit') AND result_package_id IS NULL) "
            "OR (tool_name IN ('ifc_products_apply','ifc_products_edit') AND result_package_id IS NOT NULL)) "
            "AND result_geometry_resource_version_id IS NULL) OR "
            "(operation_kind = 'geometry_resource_write' "
            "AND result_package_id IS NULL "
            "AND result_geometry_resource_version_id IS NOT NULL "
            "AND result_product_family_version_id IS NULL) OR "
            "(operation_kind = 'product_family_write' "
            "AND result_package_id IS NULL "
            "AND result_geometry_resource_version_id IS NULL "
            "AND result_product_family_version_id IS NOT NULL) OR "
            "(operation_kind = 'geometry_inspection' AND result_package_id IS NULL "
            "AND result_geometry_resource_version_id IS NULL "
            "AND result_product_family_version_id IS NULL "
            "AND inspection_result_artifact_version_id IS NOT NULL))) OR "
            "(status = 'failed' AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL "
            "AND (result_package_id IS NULL OR (operation_kind='ifc_model_write' "
            "AND tool_name IN ('ifc_products_apply','ifc_products_edit'))) "
            "AND result_geometry_resource_version_id IS NULL "
            "AND result_product_family_version_id IS NULL) OR "
            "(status = 'indeterminate' AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL "
            "AND error_phase = 'reconcile' AND result_package_id IS NULL "
            "AND result_geometry_resource_version_id IS NULL "
            "AND result_product_family_version_id IS NULL) OR "
            "(status='succeeded' AND finished_at IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL AND error_phase IS NULL "
            "AND (operation_kind='authoring_publish' OR operation_detail_json ->> 'detail_kind'='authoring_draft_write'))",
            name="terminal_result_valid",
        ),
        CheckConstraint(
            "tool_name <> 'ifc_products_edit' OR COALESCE((operation_kind='ifc_model_write' AND "
            "operation_detail_json ->> 'detail_kind'='ifc_products_edit' AND operation_detail_json -> "
            "'request' ->> 'base_revision_id'=base_ifc_revision_id AND operation_detail_json -> 'request' "
            "->> 'package_id'=operation_detail_json -> 'input_binding' ->> 'package_id' AND "
            "operation_detail_json -> 'request' ->> 'payload_name'=operation_detail_json -> 'input_binding' "
            "->> 'payload_name' AND operation_detail_json -> 'input_binding' ->> 'member_id' IS NOT NULL AND"
            " ((status IN ('prepared','indeterminate') AND operation_detail_json -> 'result' IS NULL AND "
            "result_package_id IS NULL) OR (status='succeeded' AND result_package_id IS NOT NULL AND "
            "operation_detail_json -> 'result' IS NOT NULL AND (operation_detail_json -> 'result' ->> "
            "'committed_count')::bigint=(operation_detail_json -> 'result' ->> 'input_count')::bigint) OR "
            "(status='failed' AND ((result_package_id IS NULL AND operation_detail_json -> 'result' IS NULL)"
            " OR (result_package_id IS NOT NULL AND (operation_detail_json -> 'result' ->> "
            "'committed_count')::bigint=0 AND (operation_detail_json -> 'result' ->> "
            "'created_product_count')::bigint=0 AND (operation_detail_json -> 'result' ->> "
            "'deleted_product_count')::bigint=0))))), false)",
            name="products_edit_detail_valid",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "tool_name<>'ifc_products_apply' OR COALESCE(("
            "operation_kind='ifc_model_write' AND "
            "operation_detail_json ->> 'detail_kind'='ifc_products_apply' "
            "AND operation_detail_json -> 'request' ->> 'family_version_id' IS NOT NULL "
            "AND operation_detail_json -> 'input_binding' ->> 'member_id' IS NOT NULL "
            "AND operation_detail_json -> 'request' ->> 'package_id' = "
            "operation_detail_json -> 'input_binding' ->> 'package_id' "
            "AND operation_detail_json -> 'request' ->> 'payload_name' = "
            "operation_detail_json -> 'input_binding' ->> 'payload_name' "
            "AND ((status IN ('prepared','indeterminate') AND "
            "(operation_detail_json -> 'result' IS NULL OR operation_detail_json -> 'result'='null'::jsonb)) "
            "OR (status='succeeded' AND result_package_id IS NOT NULL AND "
            "operation_detail_json -> 'result' ->> 'outcome'='committed') "
            "OR (status='failed' AND ((result_package_id IS NULL AND "
            "(operation_detail_json -> 'result' IS NULL OR operation_detail_json -> 'result'='null'::jsonb)) "
            "OR (result_package_id IS NOT NULL AND "
            "operation_detail_json -> 'result' ->> 'outcome' IN ('rejected','technical_failure')))))), FALSE)",
            name="products_apply_detail_valid",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(status IN ('prepared', 'succeeded') AND error_phase IS NULL) OR "
            "(status = 'indeterminate' AND error_phase = 'reconcile') OR "
            "(status = 'failed' AND operation_kind = 'source_ingestion' "
            "AND (error_phase IS NULL OR error_phase = 'source_extraction')) OR "
            "(status = 'failed' AND operation_kind = 'data_transform' "
            "AND error_phase IN ('code_validation', 'execution', 'output_validation', 'commit_fence')) OR "
            "(status = 'failed' AND operation_kind = 'ifc_model_write' "
            "AND error_phase IN "
            "('ifc_validation', 'storage_verification', 'commit_fence')) OR "
            "(status = 'failed' AND operation_kind = 'geometry_resource_write' "
            "AND error_phase IN ('code_validation', 'execution', "
            "'output_validation', 'storage_verification', 'commit_fence')) OR "
            "(status = 'failed' AND operation_kind IN ('product_family_write', 'geometry_inspection', 'authoring_publish') "
            "AND error_phase IN ('code_validation', 'execution', "
            "'output_validation', 'storage_verification', 'commit_fence'))",
            name="error_phase_valid",
        ),
        CheckConstraint(
            "(base_ifc_revision_id IS NULL) = (expected_ifc_write_epoch IS NULL)",
            name="ifc_write_expectation_pair_valid",
        ),
        CheckConstraint(
            "expected_ifc_write_epoch IS NULL OR expected_ifc_write_epoch > 0",
            name="expected_ifc_write_epoch_positive",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="time_order_valid",
        ),
        Index("ix_critical_operations_run_created", "run_id", "created_at"),
    )

    operation_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    initiator: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    intent_summary: Mapped[str] = mapped_column(String(1303), nullable=False)
    source_filename: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_package_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    error_phase: Mapped[str | None] = mapped_column(String(32))
    code_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    code_artifact_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    code_artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    inspection_request_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    inspection_request_artifact_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    inspection_request_artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    inspection_result_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    inspection_result_artifact_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    inspection_result_artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    inspection_source_geometry_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    inspection_source_family_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    description_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    description_artifact_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    description_artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    source_ifc_model_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    source_ifc_revision_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    repair_parent_operation_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    base_ifc_revision_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    expected_ifc_write_epoch: Mapped[int | None] = mapped_column(BigInteger)
    ifc_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ifc_execution_owner: Mapped[str | None] = mapped_column(String(160))
    ifc_execution_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ifc_execution_token_sha256: Mapped[str | None] = mapped_column(String(64))
    ifc_guard_pid: Mapped[int | None] = mapped_column(BigInteger)
    ifc_guard_birth_us: Mapped[int | None] = mapped_column(BigInteger)
    ifc_candidate_json: Mapped[dict | None] = mapped_column(_json_type())
    target_geometry_resource_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    result_geometry_resource_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    target_product_family_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    base_product_family_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    result_product_family_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    operation_detail_json: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    transform_request_identity: Mapped[str | None] = mapped_column(String(255))
    transform_request_digest: Mapped[str | None] = mapped_column(String(64))
    transform_request_json: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    transform_resolved_json: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    transform_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transform_execution_owner: Mapped[str | None] = mapped_column(String(160))
    transform_execution_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transform_execution_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transform_execution_token_sha256: Mapped[str | None] = mapped_column(String(64))
    transform_candidate_json: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    authoring_execution_owner: Mapped[str | None] = mapped_column(String(160))
    authoring_execution_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    authoring_execution_token_sha256: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CriticalOperationInputModel(Base, CreatedAtMixin, TenantScopedMixin):
    __tablename__ = "critical_operation_inputs"
    __table_args__ = (
        _tenant_task_scope_fk("critical_operation_inputs"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "operation_id"],
            [
                "critical_operations.owner_id",
                "critical_operations.task_id",
                "critical_operations.operation_id",
            ],
            name="fk_critical_operation_inputs_operation",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "package_id", "member_id"],
            [
                "data_package_members.owner_id",
                "data_package_members.task_id",
                "data_package_members.package_id",
                "data_package_members.member_id",
            ],
            name="fk_critical_operation_inputs_exact_member",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("operation_id", "ordinal", name="uq_critical_operation_inputs_ordinal"),
        UniqueConstraint("operation_id", "input_name", name="uq_critical_operation_inputs_name"),
        UniqueConstraint(
            "operation_id",
            "package_id",
            "member_id",
            name="uq_critical_operation_inputs_member",
        ),
        CheckConstraint("ordinal > 0 AND ordinal <= 16", name="ordinal_valid"),
    )

    input_binding_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    input_name: Mapped[str] = mapped_column(String(63), nullable=False)
    package_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    member_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    payload_name: Mapped[str] = mapped_column(String(120), nullable=False)


class IfcModelModel(Base, UpdatedAtMixin, TenantScopedMixin):
    __tablename__ = "ifc_models"
    __table_args__ = (
        _tenant_task_scope_fk("ifc_models"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_ifc_models_owner_task_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "owner_id",
                "task_id",
                "run_id",
                "ifc_model_id",
                "current_revision_id",
            ],
            [
                "ifc_model_revisions.owner_id",
                "ifc_model_revisions.task_id",
                "ifc_model_revisions.run_id",
                "ifc_model_revisions.ifc_model_id",
                "ifc_model_revisions.revision_id",
            ],
            name="fk_ifc_models_current_revision",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "ifc_model_id",
            name="uq_ifc_models_owner_task_run_model",
        ),
        UniqueConstraint("run_id", name="uq_ifc_models_run"),
        CheckConstraint("ifc_schema = 'IFC4X3_ADD2'", name="ifc_schema_valid"),
        CheckConstraint(
            "length(project_global_id) = 22",
            name="project_global_id_length_valid",
        ),
        CheckConstraint("write_epoch > 0", name="write_epoch_positive"),
        CheckConstraint("status IN ('active', 'frozen')", name="status_valid"),
        CheckConstraint(
            "(status = 'active' AND freeze_reason_code IS NULL) OR "
            "(status = 'frozen' AND freeze_reason_code IS NOT NULL)",
            name="freeze_reason_valid",
        ),
        Index("ix_ifc_models_run_current", "run_id", "current_revision_id"),
    )

    ifc_model_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    ifc_schema: Mapped[str] = mapped_column(String(32), nullable=False)
    project_global_id: Mapped[str] = mapped_column(String(22), nullable=False)
    current_revision_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    write_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    freeze_reason_code: Mapped[str | None] = mapped_column(String(120))


class IfcModelRevisionModel(Base, CreatedAtMixin, TenantScopedMixin):
    __tablename__ = "ifc_model_revisions"
    __table_args__ = (
        _tenant_task_scope_fk("ifc_model_revisions"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_ifc_model_revisions_owner_task_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "ifc_model_id"],
            [
                "ifc_models.owner_id",
                "ifc_models.task_id",
                "ifc_models.run_id",
                "ifc_models.ifc_model_id",
            ],
            name="fk_ifc_model_revisions_model",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "artifact_id"],
            [
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ],
            name="fk_ifc_model_revisions_artifact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "artifact_id", "artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_ifc_model_revisions_exact_artifact_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "operation_id"],
            [
                "critical_operations.owner_id",
                "critical_operations.task_id",
                "critical_operations.run_id",
                "critical_operations.operation_id",
            ],
            name="fk_ifc_model_revisions_operation",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "ifc_model_id",
            "revision_id",
            name="uq_ifc_model_revisions_owner_task_run_model_revision",
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "revision_id",
            name="uq_ifc_model_revisions_owner_task_run_revision",
        ),
        UniqueConstraint(
            "ifc_model_id",
            "revision_no",
            name="uq_ifc_model_revisions_model_revision_no",
        ),
        UniqueConstraint("operation_id", name="uq_ifc_model_revisions_operation"),
        CheckConstraint("revision_no > 0", name="revision_no_positive"),
        Index(
            "ix_ifc_model_revisions_model_created",
            "ifc_model_id",
            "created_at",
        ),
    )

    revision_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    ifc_model_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    revision_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    artifact_version_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)


class DataCatalogEntryModel(Base, CreatedAtMixin, TenantScopedMixin):
    __tablename__ = "data_catalog_entries"
    __table_args__ = (
        _tenant_task_scope_fk("data_catalog_entries"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_data_catalog_entries_owner_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "package_id"],
            ["data_packages.owner_id", "data_packages.task_id", "data_packages.package_id"],
            name="fk_data_catalog_entries_package",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "operation_id"],
            [
                "critical_operations.owner_id",
                "critical_operations.task_id",
                "critical_operations.operation_id",
            ],
            name="fk_data_catalog_entries_operation",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "alias", name="uq_data_catalog_entries_run_alias"),
        UniqueConstraint(
            "run_id", "resource_kind", "ordinal", name="uq_data_catalog_entries_run_ordinal"
        ),
        UniqueConstraint("run_id", "package_id", name="uq_data_catalog_entries_run_package"),
        UniqueConstraint("run_id", "operation_id", name="uq_data_catalog_entries_run_operation"),
        CheckConstraint("resource_kind IN ('package', 'operation')", name="resource_kind_valid"),
        CheckConstraint("ordinal > 0", name="ordinal_positive"),
        CheckConstraint("catalog_revision > 0", name="catalog_revision_positive"),
        CheckConstraint(
            "(resource_kind = 'package' AND package_id IS NOT NULL AND operation_id IS NULL) "
            "OR (resource_kind = 'operation' AND operation_id IS NOT NULL AND package_id IS NULL)",
            name="resource_target_valid",
        ),
        Index(
            "ix_data_catalog_entries_run_revision",
            "run_id",
            "catalog_revision",
            "created_at",
        ),
        Index(
            "ix_data_catalog_entries_run_kind_ordinal",
            "run_id",
            "resource_kind",
            "ordinal",
        ),
    )

    catalog_entry_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    alias: Mapped[str] = mapped_column(String(32), nullable=False)
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    catalog_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    package_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    operation_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))


class OpaqueReferenceIndexModel(Base, CreatedAtMixin, TenantScopedMixin):
    """Immutable navigation only; sealed references retain their domain authority."""

    __tablename__ = "opaque_reference_index"
    __table_args__ = (
        _tenant_task_scope_fk("opaque_reference_index"),
        ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["tenant_principals.tenant_id", "tenant_principals.principal_id"],
            name="fk_opaque_reference_index_tenant_principal", ondelete="CASCADE",
        ),
        UniqueConstraint(
            "scope_digest", "reference_sha256", name="uq_opaque_reference_index_scope_target",
        ),
        CheckConstraint(
            "reference_kind IN ('gr','gv','fr','fv','sc','gp','pp','gq','gi','gd','fd','or')",
            name="reference_kind_valid",
        ),
        CheckConstraint("length(handle) = 25", name="handle_length_valid"),
        CheckConstraint("length(scope_digest) = 64", name="scope_digest_length_valid"),
        CheckConstraint("length(reference_sha256) = 64", name="reference_sha256_length_valid"),
        CheckConstraint("length(sealed_reference) BETWEEN 8 AND 16384", name="reference_length_valid"),
    )

    handle: Mapped[str] = mapped_column(String(25), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_kind: Mapped[str] = mapped_column(String(2), nullable=False)
    reference_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sealed_reference: Mapped[str] = mapped_column(Text, nullable=False)


class GeometryResourceModel(Base, CreatedAtMixin):
    """Stable SYSTEM, TENANT, or TASK identity for reusable geometry."""

    __tablename__ = "geometry_resources"
    __table_args__ = (
        CheckConstraint("owner_scope <> 'SYSTEM' OR deleted_at IS NULL", name="system_not_deleted"),
        CheckConstraint(
            "(draft_generation = 0 AND draft_artifact_version_id IS NULL) OR "
            "(draft_generation > 0 AND draft_artifact_version_id IS NOT NULL)",
            name="draft_binding_consistent",
        ),
        ForeignKeyConstraint(
            ["owner_tenant_id", "owner_task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_geometry_resources_owner_tenant_task_tasks",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "resource_kind IN ('mapped_model', 'modeling_function')",
            name="resource_kind_valid",
        ),
        CheckConstraint(
            "owner_scope IN ('SYSTEM', 'TENANT', 'TASK')",
            name="owner_scope_valid",
        ),
        CheckConstraint(
            "(owner_scope = 'SYSTEM' AND owner_tenant_id IS NULL "
            "AND owner_task_id IS NULL) OR "
            "(owner_scope = 'TENANT' AND owner_tenant_id IS NOT NULL "
            "AND owner_task_id IS NULL) OR "
            "(owner_scope = 'TASK' AND owner_tenant_id IS NOT NULL "
            "AND owner_task_id IS NOT NULL)",
            name="owner_identity_valid",
        ),
        CheckConstraint(
            "display_name <> '' AND display_name = trim(display_name)",
            name="display_name_valid",
        ),
        Index(
            "ix_geometry_resources_scope_kind_created",
            "owner_scope",
            "owner_tenant_id",
            "owner_task_id",
            "resource_kind",
            "created_at",
        ),
    )

    resource_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    draft_artifact_version_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("artifact_versions.version_id", ondelete="RESTRICT"),
    )
    draft_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    owner_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_tenant_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
    )
    owner_task_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)


class GeometryResourceVersionModel(Base, CreatedAtMixin):
    """Immutable exact version of one GeometryResource."""

    __tablename__ = "geometry_resource_versions"
    __table_args__ = (
        UniqueConstraint(
            "resource_id",
            "version_no",
            name="uq_geometry_resource_versions_resource_version_no",
        ),
        UniqueConstraint(
            "resource_id",
            "resource_version_id",
            name="uq_geometry_resource_versions_resource_exact_version",
        ),
        CheckConstraint("version_no > 0", name="version_no_positive"),
        CheckConstraint(
            "origin_kind <> '' AND origin_kind = trim(origin_kind)",
            name="origin_kind_valid",
        ),
        CheckConstraint(
            "length(content_digest) = 64 AND content_digest = lower(content_digest) "
            f"AND {_GEOMETRY_CONTENT_DIGEST_REMAINDER} = ''",
            name="content_digest_shape",
        ),
        CheckConstraint(
            "length(manifest_digest) = 64 AND manifest_digest = lower(manifest_digest) "
            f"AND {_GEOMETRY_MANIFEST_DIGEST_REMAINDER} = ''",
            name="manifest_digest_shape",
        ),
        Index(
            "ix_geometry_resource_versions_resource_created",
            "resource_id",
            "created_at",
        ),
    )

    resource_version_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    resource_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("geometry_resources.resource_id", ondelete="RESTRICT"),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    lineage_json: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)


class GeometryResourceVersionInputModel(Base):
    """Foreign-key projection of the owning version's manifest inputs."""

    __tablename__ = "geometry_resource_version_inputs"
    __table_args__ = (
        CheckConstraint("input_key ~ '^[a-z][a-z0-9_]{0,63}$'", name="input_key_valid"),
        Index("ix_geometry_resource_version_inputs_target", "target_resource_version_id"),
    )
    resource_version_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("geometry_resource_versions.resource_version_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    input_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_resource_version_id: Mapped[str] = mapped_column(
        String(ID_LENGTH), ForeignKey("geometry_resource_versions.resource_version_id", ondelete="RESTRICT"),
        nullable=False,
    )


class GeometryResourceVersionAssetModel(Base, CreatedAtMixin):
    """Exact immutable byte binding owned by one geometry resource version."""

    __tablename__ = "geometry_resource_version_assets"
    __table_args__ = (
        CheckConstraint(
            "asset_role IN ('canonical_geometry', 'manifest', 'validation_report', "
            "'source_bundle', 'parameter_schema', 'preview', "
            "'inspection', 'sample_output')",
            name="asset_role_valid",
        ),
        CheckConstraint(
            "asset_format <> '' AND asset_format = trim(asset_format)",
            name="asset_format_valid",
        ),
        CheckConstraint(
            "content_type <> '' AND content_type = trim(content_type)",
            name="content_type_valid",
        ),
        CheckConstraint(
            "filename <> '' AND filename = trim(filename)",
            name="filename_valid",
        ),
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
        CheckConstraint(
            "length(sha256) = 64 AND sha256 = lower(sha256) "
            f"AND {_GEOMETRY_ASSET_SHA256_REMAINDER} = ''",
            name="sha256_shape",
        ),
        Index(
            "ix_geometry_resource_version_assets_sha256",
            "sha256",
        ),
    )

    resource_version_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey(
            "geometry_resource_versions.resource_version_id",
            name="fk_geometry_resource_assets_version",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    asset_role: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_format: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class ProductFamilyModel(Base, CreatedAtMixin):
    """Stable SYSTEM, TENANT, or TASK ProductFamily identity."""

    __tablename__ = "product_families"
    __table_args__ = (
        CheckConstraint("owner_scope <> 'SYSTEM' OR deleted_at IS NULL", name="system_not_deleted"),
        CheckConstraint(
            "(draft_generation = 0 AND draft_artifact_version_id IS NULL) OR "
            "(draft_generation > 0 AND draft_artifact_version_id IS NOT NULL)",
            name="draft_binding_consistent",
        ),
        ForeignKeyConstraint(
            ["owner_tenant_id", "owner_task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            name="fk_product_families_owner_tenant_task_tasks",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "owner_scope IN ('SYSTEM', 'TENANT', 'TASK')",
            name="owner_scope_valid",
        ),
        CheckConstraint(
            "(owner_scope = 'SYSTEM' AND owner_tenant_id IS NULL "
            "AND owner_task_id IS NULL) OR "
            "(owner_scope = 'TENANT' AND owner_tenant_id IS NOT NULL "
            "AND owner_task_id IS NULL) OR "
            "(owner_scope = 'TASK' AND owner_tenant_id IS NOT NULL "
            "AND owner_task_id IS NOT NULL)",
            name="owner_identity_valid",
        ),
        Index(
            "ix_product_families_scope_created",
            "owner_scope",
            "owner_tenant_id",
            "owner_task_id",
            "created_at",
        ),
    )

    family_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    draft_artifact_version_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH), ForeignKey("artifact_versions.version_id", ondelete="RESTRICT"),
    )
    draft_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    owner_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_tenant_id: Mapped[str | None] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
    )
    owner_task_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))


class ProductFamilyVersionModel(Base, CreatedAtMixin):
    """Immutable exact ProductFamily contract and realization identity."""

    __tablename__ = "product_family_versions"
    __table_args__ = (
        UniqueConstraint(
            "family_id",
            "version_no",
            name="uq_product_family_versions_family_version_no",
        ),
        UniqueConstraint(
            "family_id",
            "family_version_id",
            name="uq_product_family_versions_family_exact_version",
        ),
        ForeignKeyConstraint(
            ["family_id", "parent_family_version_id"],
            [
                "product_family_versions.family_id",
                "product_family_versions.family_version_id",
            ],
            name="fk_product_family_versions_same_family_parent",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("version_no > 0", name="version_no_positive"),
        CheckConstraint(
            "parent_family_version_id IS NULL OR parent_family_version_id <> family_version_id",
            name="parent_not_self",
        ),
        CheckConstraint(
            "display_name <> '' AND display_name = trim(display_name)",
            name="display_name_valid",
        ),
        CheckConstraint(
            "purpose <> '' AND purpose = trim(purpose)",
            name="purpose_valid",
        ),
        CheckConstraint(
            "usage_instructions IS NULL OR ("
            "usage_instructions <> '' "
            "AND char_length(usage_instructions) <= 8192 "
            "AND octet_length(usage_instructions) <= 32768)",
            name="usage_instructions_bounded",
        ),
        CheckConstraint(
            "json_type(family_contract_json) = 'object' "
            "AND family_contract_json ->> 'product_structure_kind' "
            "IN ('single_product', 'semantic_assembly')",
            name="contract_shape",
        ),
        CheckConstraint(
            "length(contract_digest) = 64 AND contract_digest = lower(contract_digest) "
            f"AND {_PRODUCT_FAMILY_CONTRACT_DIGEST_REMAINDER} = ''",
            name="contract_digest_shape",
        ),
        CheckConstraint(
            "length(content_digest) = 64 AND content_digest = lower(content_digest) "
            f"AND {_PRODUCT_FAMILY_CONTENT_DIGEST_REMAINDER} = ''",
            name="content_digest_shape",
        ),
        CheckConstraint(
            "program_entry = 'main.py:realize'",
            name="program_entry_valid",
        ),
        Index(
            "ix_product_family_versions_family_created",
            "family_id",
            "created_at",
        ),
    )

    family_version_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    family_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey("product_families.family_id", ondelete="RESTRICT"),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parent_family_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(2048), nullable=False)
    usage_instructions: Mapped[str | None] = mapped_column(Text)
    family_contract_json: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    contract_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    program_entry: Mapped[str] = mapped_column(String(64), nullable=False)


class ProductFamilyGeometryBindingModel(Base, CreatedAtMixin):
    """Family-local key bound to one exact GeometryResourceVersion pair."""

    __tablename__ = "product_family_geometry_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["geometry_resource_id", "geometry_resource_version_id"],
            [
                "geometry_resource_versions.resource_id",
                "geometry_resource_versions.resource_version_id",
            ],
            name="fk_product_family_bindings_exact_geometry_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(binding_key) BETWEEN 1 AND 64 "
            "AND substr(binding_key, 1, 1) BETWEEN 'a' AND 'z' "
            f"AND {_PRODUCT_FAMILY_BINDING_KEY_REMAINDER} = ''",
            name="binding_key_valid",
        ),
        CheckConstraint(
            "length(geometry_content_digest) = 64 "
            "AND geometry_content_digest = lower(geometry_content_digest) "
            f"AND {_PRODUCT_FAMILY_GEOMETRY_DIGEST_REMAINDER} = ''",
            name="geometry_digest_shape",
        ),
        CheckConstraint(
            "expected_resource_kind IN ('mapped_model', 'modeling_function')",
            name="resource_kind_valid",
        ),
        CheckConstraint(
            "geometry_purpose IN ('product_component', 'shape_fragment', 'interface_adapter')",
            name="geometry_purpose_valid",
        ),
        Index(
            "ix_product_family_bindings_geometry_version",
            "geometry_resource_id",
            "geometry_resource_version_id",
        ),
    )

    family_version_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey(
            "product_family_versions.family_version_id",
            name="fk_product_family_bindings_family_version",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    binding_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    geometry_resource_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    geometry_resource_version_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    geometry_content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    geometry_purpose: Mapped[str] = mapped_column(String(32), nullable=False)


class ProductFamilyVersionAssetModel(Base, CreatedAtMixin):
    """Exact immutable byte metadata owned by one ProductFamilyVersion."""

    __tablename__ = "product_family_version_assets"
    __table_args__ = (
        CheckConstraint(
            "asset_role IN ('realization_source_bundle', 'validation_report', "
            "'standard_model_geometry', 'standard_model_manifest')",
            name="asset_role_valid",
        ),
        CheckConstraint(
            "asset_format <> '' AND asset_format = trim(asset_format)",
            name="asset_format_valid",
        ),
        CheckConstraint(
            "filename <> '' AND filename = trim(filename) "
            "AND length(filename) = length(replace(filename, '/', ''))",
            name="filename_valid",
        ),
        CheckConstraint(
            "content_type <> '' AND content_type = trim(content_type)",
            name="content_type_valid",
        ),
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
        CheckConstraint(
            "length(sha256) = 64 AND sha256 = lower(sha256) "
            f"AND {_PRODUCT_FAMILY_ASSET_SHA256_REMAINDER} = ''",
            name="sha256_shape",
        ),
        CheckConstraint(
            "asset_role <> 'realization_source_bundle' OR "
            "(asset_format = 'source_bundle_v1' AND content_type = 'application/zip')",
            name="source_bundle_shape",
        ),
        Index("ix_product_family_version_assets_sha256", "sha256"),
    )

    family_version_id: Mapped[str] = mapped_column(
        String(ID_LENGTH),
        ForeignKey(
            "product_family_versions.family_version_id",
            name="fk_product_family_assets_family_version",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    asset_role: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_format: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class DataQueryAttemptModel(Base, CreatedAtMixin, TenantScopedMixin):
    """Append-only Run-scoped query identity with one controlled terminal transition."""

    __tablename__ = "data_query_attempts"
    __table_args__ = (
        _tenant_task_scope_fk("data_query_attempts"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_data_query_attempts_owner_task_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "source_artifact_id"],
            [
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ],
            name="fk_data_query_attempts_run_source_artifact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "source_artifact_id", "source_artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_data_query_attempts_exact_source_artifact_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "repair_parent_attempt_id", "engine"],
            [
                "data_query_attempts.owner_id",
                "data_query_attempts.task_id",
                "data_query_attempts.run_id",
                "data_query_attempts.attempt_id",
                "data_query_attempts.engine",
            ],
            name="fk_data_query_attempts_repair_parent",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "attempt_id",
            name="uq_data_query_attempts_run_attempt",
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "attempt_id",
            "engine",
            name="uq_data_query_attempts_run_attempt_engine",
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "request_identity",
            name="uq_data_query_attempts_run_request_identity",
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "sequence",
            name="uq_data_query_attempts_run_sequence",
        ),
        CheckConstraint("engine IN ('duckdb_sql', 'python_arrow')", name="engine_valid"),
        CheckConstraint("status IN ('PREPARED', 'SUCCEEDED', 'FAILED')", name="status_valid"),
        CheckConstraint(
            "request_identity <> '' AND request_identity = trim(request_identity)",
            name="request_identity_valid",
        ),
        CheckConstraint(
            "length(request_digest) = 64 AND request_digest = lower(request_digest) "
            f"AND {_DATA_QUERY_REQUEST_DIGEST_REMAINDER} = ''",
            name="request_digest_shape",
        ),
        CheckConstraint("input_count BETWEEN 1 AND 16", name="input_count_valid"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint("page_size BETWEEN 1 AND 200", name="page_size_valid"),
        CheckConstraint(
            "parameters_json IS NOT NULL",
            name="parameters_valid",
        ),
        CheckConstraint(
            "inline_tables_json IS NOT NULL",
            name="inline_tables_valid",
        ),
        CheckConstraint(
            "engine IN ('duckdb_sql', 'python_arrow')",
            name="inline_tables_python_only",
        ),
        CheckConstraint(
            "repair_parent_attempt_id IS NULL OR repair_parent_attempt_id <> attempt_id",
            name="repair_parent_not_self",
        ),
        CheckConstraint(
            "(status = 'PREPARED' AND diagnostic_json IS NULL AND result_summary_json IS NULL "
            "AND finished_at IS NULL) OR "
            "(status = 'SUCCEEDED' AND diagnostic_json IS NULL AND result_summary_json IS NOT NULL "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'FAILED' AND diagnostic_json IS NOT NULL AND result_summary_json IS NULL "
            "AND finished_at IS NOT NULL)",
            name="terminal_shape_valid",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= created_at", name="time_order_valid"
        ),
        Index(
            "ix_data_query_attempts_run_engine_created",
            "owner_id",
            "task_id",
            "run_id",
            "engine",
            "created_at",
            "attempt_id",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    source_artifact_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    source_artifact_version_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    repair_parent_attempt_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    request_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_count: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters_json: Mapped[list[dict[str, Any]]] = mapped_column(_json_type(), nullable=False)
    inline_tables_json: Mapped[list[dict[str, Any]]] = mapped_column(_json_type(), nullable=False)
    page_size: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    diagnostic_json: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    result_summary_json: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DataQueryInputModel(Base, CreatedAtMixin, TenantScopedMixin):
    """Exact immutable Package/Payload binding owned by one DataQueryAttempt."""

    __tablename__ = "data_query_inputs"
    __table_args__ = (
        _tenant_task_scope_fk("data_query_inputs"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "attempt_id"],
            [
                "data_query_attempts.owner_id",
                "data_query_attempts.task_id",
                "data_query_attempts.run_id",
                "data_query_attempts.attempt_id",
            ],
            name="fk_data_query_inputs_attempt",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "package_id", "payload_member_id"],
            [
                "data_package_members.owner_id",
                "data_package_members.task_id",
                "data_package_members.package_id",
                "data_package_members.member_id",
            ],
            name="fk_data_query_inputs_payload_member",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "package_id", "schema_member_id"],
            [
                "data_package_members.owner_id",
                "data_package_members.task_id",
                "data_package_members.package_id",
                "data_package_members.member_id",
            ],
            name="fk_data_query_inputs_schema_member",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal BETWEEN 1 AND 16", name="ordinal_valid"),
        CheckConstraint(
            "length(input_name) BETWEEN 1 AND 63",
            name="input_name_valid",
        ),
        UniqueConstraint("attempt_id", "input_name", name="uq_data_query_inputs_attempt_name"),
        UniqueConstraint(
            "attempt_id",
            "package_id",
            "payload_member_id",
            name="uq_data_query_inputs_attempt_payload",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    input_name: Mapped[str] = mapped_column(String(63), nullable=False)
    package_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    payload_member_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    schema_member_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))


class GeometryQueryAttemptModel(Base, CreatedAtMixin, TenantScopedMixin):
    """Append-only Task query identity with one controlled terminal transition."""

    __tablename__ = "geometry_query_attempts"
    __table_args__ = (
        _tenant_task_scope_fk("geometry_query_attempts"),
        ForeignKeyConstraint(
            ["origin_run_id"],
            ["runs.run_id"],
            name="fk_geometry_query_attempts_origin_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["resource_version_id"],
            ["geometry_resource_versions.resource_version_id"],
            name="fk_geometry_query_attempts_resource_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["family_version_id"],
            ["product_family_versions.family_version_id"],
            name="fk_geometry_query_attempts_family_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["target_artifact_version_id"],
            ["artifact_versions.version_id"],
            name="fk_geometry_query_attempts_target_artifact_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_artifact_version_id"],
            ["artifact_versions.version_id"],
            name="fk_geometry_query_attempts_source_artifact_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["result_artifact_version_id"],
            ["artifact_versions.version_id"],
            name="fk_geometry_query_attempts_result_artifact_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "task_id", "repair_parent_attempt_id"],
            [
                "geometry_query_attempts.tenant_id",
                "geometry_query_attempts.task_id",
                "geometry_query_attempts.attempt_id",
            ],
            name="fk_geometry_query_attempts_repair_parent",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "task_id",
            "attempt_id",
            name="uq_geometry_query_attempts_tenant_task_attempt",
        ),
        UniqueConstraint(
            "tenant_id",
            "task_id",
            "request_identity",
            name="uq_geometry_query_attempts_task_request_identity",
        ),
        CheckConstraint(
            "status IN ('PREPARED', 'SUCCEEDED', 'FAILED')",
            name="status_valid",
        ),
        CheckConstraint(
            "request_identity <> '' AND request_identity = trim(request_identity)",
            name="request_identity_valid",
        ),
        CheckConstraint(
            "(target_kind = 'family_standard_model' AND family_version_id IS NOT NULL "
            "AND resource_version_id IS NULL AND target_artifact_version_id IS NULL "
            "AND target_geometry_format IS NULL AND inspection_selection_json IS NULL "
            "AND inspection_coordinate_space_json IS NULL) OR "
            "(family_version_id IS NULL AND ((target_kind = 'geometry_resource_version' "
            "AND resource_version_id IS NOT NULL "
            "AND target_artifact_version_id IS NULL "
            "AND target_geometry_format IS NULL "
            "AND inspection_selection_json IS NULL "
            "AND inspection_coordinate_space_json IS NULL) OR "
            "(target_kind = 'geometry_inspection' "
            "AND resource_version_id IS NULL "
            "AND target_artifact_version_id IS NOT NULL "
            "AND target_geometry_format IS NULL "
            "AND inspection_selection_json IS NOT NULL "
            "AND inspection_coordinate_space_json IS NOT NULL) OR "
            "(target_kind IN ('product_realization_preview', 'geometry_preview') "
            "AND resource_version_id IS NULL "
            "AND target_artifact_version_id IS NOT NULL "
            "AND target_geometry_format = 'occt_brep_v1' "
            "AND inspection_selection_json IS NULL "
            "AND inspection_coordinate_space_json IS NULL)))",
            name="target_shape_valid",
        ),
        CheckConstraint(
            "target_kind <> 'geometry_inspection' OR ("
            "jsonb_typeof(inspection_selection_json) = 'object' AND ("
            "(inspection_selection_json ->> 'selection_kind' = 'whole' "
            "AND inspection_selection_json - 'selection_kind' = '{}'::jsonb) OR "
            "(inspection_selection_json ->> 'selection_kind' = 'component' "
            "AND inspection_selection_json ->> 'component_key' ~ '^[a-z][a-z0-9_]{0,63}$' "
            "AND inspection_selection_json - 'selection_kind' - 'component_key' = '{}'::jsonb) OR "
            "(inspection_selection_json ->> 'selection_kind' = 'fragment' "
            "AND inspection_selection_json ->> 'fragment_key' ~ '^[a-z][a-z0-9_]{0,63}$' "
            "AND inspection_selection_json - 'selection_kind' - 'fragment_key' = '{}'::jsonb)) "
            "AND jsonb_typeof(inspection_coordinate_space_json) = 'object' AND ("
            "(inspection_coordinate_space_json ->> 'coordinate_space' = 'result' "
            "AND inspection_coordinate_space_json - 'coordinate_space' = '{}'::jsonb) OR "
            "(inspection_coordinate_space_json ->> 'coordinate_space' = 'fragment_local' "
            "AND inspection_coordinate_space_json ->> 'fragment_key' ~ '^[a-z][a-z0-9_]{0,63}$' "
            "AND inspection_coordinate_space_json - 'coordinate_space' - 'fragment_key' = '{}'::jsonb "
            "AND inspection_selection_json ->> 'selection_kind' = 'fragment' "
            "AND inspection_selection_json ->> 'fragment_key' = inspection_coordinate_space_json ->> 'fragment_key'))"
            ") IS TRUE",
            name="inspection_choices_valid",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(request_digest) = 64 AND request_digest = lower(request_digest) "
            f"AND {_GEOMETRY_QUERY_REQUEST_DIGEST_REMAINDER} = ''",
            name="request_digest_shape",
        ),
        CheckConstraint(
            "repair_parent_attempt_id IS NULL OR repair_parent_attempt_id <> attempt_id",
            name="repair_parent_not_self",
        ),
        CheckConstraint(
            "json_type(diagnostics_json) = 'array'",
            name="diagnostics_array",
        ),
        CheckConstraint(
            "json_array_length(diagnostics_json) <= 32",
            name="diagnostics_count_bounded",
        ),
        CheckConstraint(
            "(status = 'PREPARED' AND result_artifact_version_id IS NULL "
            "AND diagnostics_json = '[]' AND finished_at IS NULL) OR "
            "(status = 'SUCCEEDED' AND result_artifact_version_id IS NOT NULL "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'FAILED' AND result_artifact_version_id IS NULL "
            "AND json_array_length(diagnostics_json) > 0 AND finished_at IS NOT NULL)",
            name="terminal_shape_valid",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= created_at",
            name="time_order_valid",
        ),
        Index(
            "ix_geometry_query_attempts_task_created",
            "tenant_id",
            "task_id",
            "created_at",
        ),
        Index(
            "ix_geometry_query_attempts_resource_version_created",
            "resource_version_id",
            "created_at",
        ),
        Index(
            "ix_geometry_query_attempts_target_artifact_created",
            "target_artifact_version_id",
            "created_at",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    origin_run_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    target_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    family_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    target_artifact_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    target_geometry_format: Mapped[str | None] = mapped_column(String(40))
    inspection_selection_json: Mapped[dict | None] = mapped_column(_json_type())
    inspection_coordinate_space_json: Mapped[dict | None] = mapped_column(_json_type())
    source_artifact_version_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    result_artifact_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    repair_parent_attempt_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    request_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    diagnostics_json: Mapped[list[dict[str, Any]]] = mapped_column(_json_type(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IfcQueryAttemptModel(Base, CreatedAtMixin, TenantScopedMixin):
    """Append-only exact IFC query identity with one terminal transition."""

    __tablename__ = "ifc_query_attempts"
    __table_args__ = (
        _tenant_task_scope_fk("ifc_query_attempts"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_ifc_query_attempts_owner_task_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "ifc_model_id", "ifc_revision_id"],
            [
                "ifc_model_revisions.owner_id",
                "ifc_model_revisions.task_id",
                "ifc_model_revisions.run_id",
                "ifc_model_revisions.ifc_model_id",
                "ifc_model_revisions.revision_id",
            ],
            name="fk_ifc_query_attempts_exact_ifc_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "source_artifact_id"],
            [
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ],
            name="fk_ifc_query_attempts_run_source_artifact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "result_artifact_id"],
            [
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ],
            name="fk_ifc_query_attempts_run_result_artifact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "owner_id",
                "task_id",
                "source_artifact_id",
                "source_artifact_version_id",
            ],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_ifc_query_attempts_exact_source_artifact_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "owner_id",
                "task_id",
                "result_artifact_id",
                "result_artifact_version_id",
            ],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_ifc_query_attempts_exact_result_artifact_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "owner_id",
                "task_id",
                "run_id",
                "repair_parent_attempt_id",
                "ifc_model_id",
                "ifc_revision_id",
            ],
            [
                "ifc_query_attempts.owner_id",
                "ifc_query_attempts.task_id",
                "ifc_query_attempts.run_id",
                "ifc_query_attempts.attempt_id",
                "ifc_query_attempts.ifc_model_id",
                "ifc_query_attempts.ifc_revision_id",
            ],
            name="fk_ifc_query_attempts_exact_repair_parent",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "attempt_id",
            name="uq_ifc_query_attempts_scope_attempt",
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "attempt_id",
            "ifc_model_id",
            "ifc_revision_id",
            name="uq_ifc_query_attempts_exact_revision_attempt",
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "request_identity",
            name="uq_ifc_query_attempts_run_request_identity",
        ),
        CheckConstraint(
            "status IN ('PREPARED', 'SUCCEEDED', 'FAILED')",
            name="status_valid",
        ),
        CheckConstraint(
            "request_identity <> '' AND request_identity = trim(request_identity)",
            name="request_identity_valid",
        ),
        CheckConstraint(
            "length(request_digest) = 64 AND request_digest = lower(request_digest) "
            f"AND {_IFC_QUERY_REQUEST_DIGEST_REMAINDER} = ''",
            name="request_digest_shape",
        ),
        CheckConstraint(
            "repair_parent_attempt_id IS NULL OR repair_parent_attempt_id <> attempt_id",
            name="repair_parent_not_self",
        ),
        CheckConstraint("input_count BETWEEN 0 AND 16", name="input_count_bounded"),
        CheckConstraint("json_type(budget_json) = 'object'", name="budget_object"),
        CheckConstraint("json_type(diagnostics_json) = 'array'", name="diagnostics_array"),
        CheckConstraint(
            "json_array_length(diagnostics_json) <= 32",
            name="diagnostics_count_bounded",
        ),
        CheckConstraint(
            "(result_artifact_id IS NULL) = (result_artifact_version_id IS NULL)",
            name="result_artifact_pair_valid",
        ),
        CheckConstraint(
            "(status = 'PREPARED' AND result_artifact_version_id IS NULL "
            "AND diagnostics_json = '[]' AND finished_at IS NULL) OR "
            "(status = 'SUCCEEDED' AND result_artifact_version_id IS NOT NULL "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'FAILED' AND result_artifact_version_id IS NULL "
            "AND json_array_length(diagnostics_json) > 0 AND finished_at IS NOT NULL)",
            name="terminal_shape_valid",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= created_at",
            name="time_order_valid",
        ),
        Index(
            "ix_ifc_query_attempts_scope_created",
            "owner_id",
            "task_id",
            "run_id",
            "created_at",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    ifc_model_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    ifc_revision_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    source_artifact_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    source_artifact_version_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    result_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    result_artifact_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    repair_parent_attempt_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    request_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_count: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_json: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    diagnostics_json: Mapped[list[dict[str, Any]]] = mapped_column(_json_type(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IfcQueryInputModel(Base, CreatedAtMixin, TenantScopedMixin):
    """Immutable exact Package member binding for an IFC query attempt."""

    __tablename__ = "ifc_query_inputs"
    __table_args__ = (
        _tenant_task_scope_fk("ifc_query_inputs"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "attempt_id"],
            [
                "ifc_query_attempts.owner_id",
                "ifc_query_attempts.task_id",
                "ifc_query_attempts.run_id",
                "ifc_query_attempts.attempt_id",
            ],
            name="fk_ifc_query_inputs_scope_attempt",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "package_id", "payload_member_id"],
            [
                "data_package_members.owner_id",
                "data_package_members.task_id",
                "data_package_members.package_id",
                "data_package_members.member_id",
            ],
            name="fk_ifc_query_inputs_exact_payload_member",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "package_id", "schema_member_id"],
            [
                "data_package_members.owner_id",
                "data_package_members.task_id",
                "data_package_members.package_id",
                "data_package_members.member_id",
            ],
            name="fk_ifc_query_inputs_exact_schema_member",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "attempt_id",
            "ordinal",
            name="uq_ifc_query_inputs_attempt_ordinal",
        ),
        UniqueConstraint(
            "attempt_id",
            "input_name",
            name="uq_ifc_query_inputs_attempt_name",
        ),
        UniqueConstraint(
            "attempt_id",
            "package_id",
            "payload_member_id",
            name="uq_ifc_query_inputs_attempt_payload",
        ),
        CheckConstraint("ordinal BETWEEN 1 AND 16", name="ordinal_bounded"),
        CheckConstraint(
            "length(input_name) BETWEEN 1 AND 63 "
            "AND substr(input_name, 1, 1) BETWEEN 'a' AND 'z' "
            f"AND {_IFC_QUERY_INPUT_NAME_REMAINDER} = ''",
            name="input_name_valid",
        ),
        Index("ix_ifc_query_inputs_scope_attempt", "owner_id", "task_id", "attempt_id"),
    )

    attempt_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    input_name: Mapped[str] = mapped_column(String(63), nullable=False)
    package_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    payload_member_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    schema_member_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))


class IFCRunProjectionModel(Base, UpdatedAtMixin, TenantScopedMixin):
    __tablename__ = "ifc_run_projections"
    __table_args__ = (
        _tenant_task_scope_fk("ifc_run_projections"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_ifc_run_projections_owner_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id", "final_artifact_id"],
            [
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ],
            name="fk_ifc_run_projections_final_artifact_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "final_artifact_id", "final_artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_ifc_run_projections_final_artifact_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "owner_id", "task_id", "run_id", name="uq_ifc_run_projections_owner_task_run"
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("source_checkpoint_id <> ''", name="source_checkpoint_id_nonempty"),
        CheckConstraint(
            "status IN ('pending', 'running', 'passed', 'blocked', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint(
            "(final_artifact_id IS NULL) = (final_artifact_version_id IS NULL) "
            "AND (final_artifact_id IS NULL) = (final_artifact IS NULL)",
            name="final_artifact_fields_consistent",
        ),
        CheckConstraint(
            "status != 'passed' OR (global_review IS NOT NULL AND final_artifact IS NOT NULL)",
            name="passed_fields_present",
        ),
        CheckConstraint(
            "status = 'passed' OR final_artifact IS NULL",
            name="final_artifact_only_when_passed",
        ),
        Index("ix_ifc_run_projections_owner_task", "owner_id", "task_id"),
    )

    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_checkpoint_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_checkpoint_ns: Mapped[str] = mapped_column(String(1_024), nullable=False)
    global_plan: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    current_stage_key: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    global_review: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    final_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    final_artifact_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    final_artifact: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    failure_reason: Mapped[str] = mapped_column(Text, nullable=False)


class IFCRunStageProjectionModel(Base, UpdatedAtMixin, TenantScopedMixin):
    __tablename__ = "ifc_run_stage_projections"
    __table_args__ = (
        _tenant_task_scope_fk("ifc_run_stage_projections"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            [
                "ifc_run_projections.owner_id",
                "ifc_run_projections.task_id",
                "ifc_run_projections.run_id",
            ],
            name="fk_ifc_run_stage_projections_run_projection",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "owner_id",
                "task_id",
                "run_id",
                "stage_key",
                "stage_plan_revision",
                "stage_plan_id",
            ],
            [
                "stage_plans.owner_id",
                "stage_plans.task_id",
                "stage_plans.run_id",
                "stage_plans.stage_key",
                "stage_plans.definition_revision",
                "stage_plans.plan_id",
            ],
            name="fk_ifc_run_stage_projections_stage_plan_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "owner_id",
                "task_id",
                "accepted_artifact_run_id",
                "accepted_artifact_id",
            ],
            [
                "artifacts.owner_id",
                "artifacts.task_id",
                "artifacts.run_id",
                "artifacts.artifact_id",
            ],
            name="fk_ifc_stage_projection_accepted_artifact_scope",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "owner_id",
                "task_id",
                "accepted_artifact_id",
                "accepted_artifact_version_id",
            ],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_ifc_stage_projection_accepted_artifact_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "accepted_savepoint_id"],
            [
                "checkpoint_links.owner_id",
                "checkpoint_links.task_id",
                "checkpoint_links.savepoint_id",
            ],
            name="fk_ifc_stage_projection_accepted_savepoint",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "run_id",
            "stage_key",
            name="uq_ifc_run_stage_projections_owner_task_run_stage",
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("stage_generation > 0", name="stage_generation_positive"),
        CheckConstraint(
            "length(dependency_input_digest) = 64",
            name="dependency_input_digest_shape",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'passed', 'blocked', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint(
            "(stage_plan_id IS NULL) = (stage_plan_revision IS NULL) "
            "AND (stage_plan_id IS NULL) = (accepted_tree_revision IS NULL) "
            "AND (stage_plan_id IS NULL) = (accepted_tree_fingerprint IS NULL) "
            "AND (stage_plan_id IS NULL) = (accepted_savepoint_id IS NULL)",
            name="accepted_tree_fields_consistent",
        ),
        CheckConstraint(
            "(accepted_artifact_id IS NULL) = (accepted_artifact_run_id IS NULL) "
            "AND (accepted_artifact_id IS NULL) = (accepted_artifact_version_id IS NULL) "
            "AND (accepted_artifact_id IS NULL) = (accepted_artifact IS NULL)",
            name="accepted_artifact_pair",
        ),
        CheckConstraint(
            "status != 'passed' OR (stage_plan_id IS NOT NULL "
            "AND accepted_tree_revision IS NOT NULL "
            "AND accepted_tree_fingerprint IS NOT NULL "
            "AND accepted_savepoint_id IS NOT NULL "
            "AND accepted_artifact IS NOT NULL "
            "AND context_card IS NOT NULL)",
            name="passed_fields_present",
        ),
        CheckConstraint(
            "accepted_tree_revision IS NULL OR accepted_tree_revision > 0",
            name="accepted_tree_revision_positive",
        ),
        CheckConstraint(
            "accepted_tree_fingerprint IS NULL OR "
            "(length(accepted_tree_fingerprint) = 64 "
            f"AND {_ACCEPTED_TREE_FINGERPRINT_REMAINDER} = '')",
            name="accepted_tree_fingerprint_shape",
        ),
        Index("ix_ifc_run_stage_projections_owner_run", "owner_id", "run_id"),
    )

    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    stage_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    stage_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    stage_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    dependency_input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    stage_plan_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    stage_plan_revision: Mapped[int | None] = mapped_column(Integer)
    accepted_tree_revision: Mapped[int | None] = mapped_column(Integer)
    accepted_tree_fingerprint: Mapped[str | None] = mapped_column(String(64))
    accepted_savepoint_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    accepted_artifact_run_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    accepted_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    accepted_artifact_version_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    accepted_artifact: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    context_card: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    evidence_refs: Mapped[list[str]] = mapped_column(_json_type(), nullable=False)
    failure_reason: Mapped[str] = mapped_column(Text, nullable=False)


class McpSessionModel(Base, UpdatedAtMixin, TenantScopedMixin):
    __tablename__ = "mcp_sessions"
    __table_args__ = (
        _tenant_task_scope_fk("mcp_sessions"),
        ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_mcp_sessions_owner_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_mcp_sessions_owner_task_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "owner_id", "task_id", "session_id", name="uq_mcp_sessions_owner_task_session"
        ),
        UniqueConstraint("task_id", "idempotency_key", name="uq_mcp_sessions_task_idempotency"),
        CheckConstraint("status IN ('active', 'closed', 'failed')", name="status_valid"),
        CheckConstraint(
            "(writer_id IS NULL) = (writer_lease_token IS NULL)",
            name="writer_lease_token_pair",
        ),
        CheckConstraint(
            "(writer_id IS NULL) = (writer_lease_expires_at IS NULL)",
            name="writer_lease_expiry_pair",
        ),
        CheckConstraint("writer_generation >= 0", name="writer_generation_nonnegative"),
        Index("ix_mcp_sessions_owner_task_status", "owner_id", "task_id", "status"),
    )

    session_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    server_name: Mapped[str] = mapped_column(String(64), nullable=False)
    active_filename: Mapped[str | None] = mapped_column(String(500))
    session_storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    writer_id: Mapped[str | None] = mapped_column(String(255))
    writer_lease_token: Mapped[str | None] = mapped_column(String(255))
    writer_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    writer_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    idempotency_key: Mapped[str] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class McpSnapshotModel(Base, CreatedAtMixin, TenantScopedMixin):
    __tablename__ = "mcp_snapshots"
    __table_args__ = (
        _tenant_task_scope_fk("mcp_snapshots"),
        ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_mcp_snapshots_owner_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "session_id"],
            ["mcp_sessions.owner_id", "mcp_sessions.task_id", "mcp_sessions.session_id"],
            name="fk_mcp_snapshots_owner_task_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.version_id",
            ],
            name="fk_mcp_snapshots_owner_task_artifact_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "owner_id", "task_id", "snapshot_id", name="uq_mcp_snapshots_owner_task_snapshot"
        ),
        UniqueConstraint(
            "owner_id",
            "task_id",
            "session_id",
            "snapshot_id",
            name="uq_mcp_snapshots_owner_task_session_snapshot",
        ),
        UniqueConstraint(
            "session_id", "idempotency_key", name="uq_mcp_snapshots_session_idempotency"
        ),
        CheckConstraint("checksum_algorithm = 'sha256'", name="checksum_algorithm_supported"),
        CheckConstraint("writer_generation >= 0", name="writer_generation_nonnegative"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    session_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    stage_key: Mapped[str | None] = mapped_column(String(255))
    snapshot_type: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_version_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    checksum_algorithm: Mapped[str] = mapped_column(String(16), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_metadata: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    writer_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )


class CheckpointLinkModel(Base, CreatedAtMixin, TenantScopedMixin):
    """A user-visible savepoint linking checkpoint, MCP, and artifact references."""

    __tablename__ = "checkpoint_links"
    __table_args__ = (
        _tenant_task_scope_fk("checkpoint_links"),
        ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_checkpoint_links_owner_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_checkpoint_links_owner_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "mcp_session_id"],
            ["mcp_sessions.owner_id", "mcp_sessions.task_id", "mcp_sessions.session_id"],
            name="fk_checkpoint_links_owner_task_mcp_session",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
            ["owner_id", "task_id", "manifest_artifact_id"],
            ["artifacts.owner_id", "artifacts.task_id", "artifacts.artifact_id"],
            name="fk_checkpoint_links_owner_task_manifest_artifact",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(mcp_session_id IS NULL) = (mcp_snapshot_id IS NULL)",
            name="mcp_reference_pair",
        ),
        CheckConstraint("writer_generation >= 0", name="writer_generation_nonnegative"),
        CheckConstraint(
            "(manifest_artifact_id IS NULL "
            "AND manifest_version_no IS NULL "
            "AND manifest_sha256 IS NULL) OR "
            "(manifest_artifact_id IS NOT NULL "
            "AND manifest_version_no IS NOT NULL "
            "AND manifest_sha256 IS NOT NULL)",
            name="manifest_binding_complete",
        ),
        CheckConstraint(
            "manifest_version_no IS NULL OR manifest_version_no > 0",
            name="manifest_version_positive",
        ),
        UniqueConstraint(
            "owner_id", "task_id", "savepoint_id", name="uq_checkpoint_links_owner_task_savepoint"
        ),
        UniqueConstraint(
            "run_id",
            "checkpoint_ns",
            "checkpoint_id",
            name="uq_checkpoint_links_run_checkpoint_cursor",
        ),
        UniqueConstraint("run_id", "idempotency_key", name="uq_checkpoint_links_run_idempotency"),
        Index("ix_checkpoint_links_owner_task_created", "owner_id", "task_id", "created_at"),
    )

    savepoint_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    safe_point_key: Mapped[str] = mapped_column(String(255), nullable=False)
    checkpoint_id: Mapped[str] = mapped_column(String(255), nullable=False)
    checkpoint_ns: Mapped[str] = mapped_column(
        String(1_024), nullable=False, server_default=text("''")
    )
    stage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    checkpoint_next: Mapped[list[str]] = mapped_column(_json_type(), nullable=False)
    safe_point_context: Mapped[str] = mapped_column(Text, nullable=False)
    default_route: Mapped[str] = mapped_column(String(255), nullable=False)
    mcp_session_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    mcp_snapshot_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    writer_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    artifact_ids: Mapped[list[str]] = mapped_column(_json_type(), nullable=False)
    manifest_artifact_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    manifest_version_no: Mapped[int | None] = mapped_column(BigInteger)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    is_user_visible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    idempotency_key: Mapped[str] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class SafePointPublicationModel(Base, UpdatedAtMixin, TenantScopedMixin):
    """Monotonic receipt joining one prepared snapshot to one exact Graph cursor."""

    __tablename__ = "safe_point_publications"
    __table_args__ = (
        _tenant_task_scope_fk("safe_point_publications"),
        ForeignKeyConstraint(
            ["owner_id", "task_id"],
            ["tasks.owner_id", "tasks.task_id"],
            name="fk_safe_point_publications_owner_task",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_safe_point_publications_owner_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "mcp_session_id"],
            ["mcp_sessions.owner_id", "mcp_sessions.task_id", "mcp_sessions.session_id"],
            name="fk_safe_point_publications_owner_task_mcp_session",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "mcp_session_id", "mcp_snapshot_id"],
            [
                "mcp_snapshots.owner_id",
                "mcp_snapshots.task_id",
                "mcp_snapshots.session_id",
                "mcp_snapshots.snapshot_id",
            ],
            name="fk_safe_point_publications_owner_task_mcp_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "artifact_id", "artifact_version_id"],
            [
                "artifact_versions.owner_id",
                "artifact_versions.task_id",
                "artifact_versions.artifact_id",
                "artifact_versions.version_id",
            ],
            name="fk_safe_point_publications_owner_task_artifact_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "savepoint_id"],
            [
                "checkpoint_links.owner_id",
                "checkpoint_links.task_id",
                "checkpoint_links.savepoint_id",
            ],
            name="fk_safe_point_publications_owner_task_savepoint",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "run_id",
            "idempotency_key",
            name="uq_safe_point_publications_run_idempotency",
        ),
        UniqueConstraint(
            "run_id",
            "checkpoint_ns",
            "checkpoint_id",
            name="uq_safe_point_publications_run_checkpoint_cursor",
        ),
        UniqueConstraint(
            "savepoint_id",
            name="uq_safe_point_publications_savepoint",
        ),
        CheckConstraint("writer_generation >= 0", name="writer_generation_nonnegative"),
        CheckConstraint(
            "phase IN ('prepared', 'checkpointed', 'linked')",
            name="phase_valid",
        ),
        CheckConstraint("length(checksum) = 64", name="checksum_length"),
        CheckConstraint(
            "checkpoint_map_digest IS NULL OR length(checkpoint_map_digest) = 64",
            name="checkpoint_digest_length",
        ),
        CheckConstraint(
            "((source_checkpoint_id IS NULL AND source_checkpoint_ns IS NULL "
            "AND checkpoint_as_node IS NULL) OR "
            "(source_checkpoint_id IS NOT NULL AND source_checkpoint_ns IS NOT NULL))",
            name="source_cursor_consistent",
        ),
        CheckConstraint(
            "((checkpoint_id IS NULL AND checkpoint_ns IS NULL "
            "AND root_checkpoint_id IS NULL AND checkpoint_map IS NULL "
            "AND checkpoint_map_digest IS NULL AND checkpoint_next IS NULL) OR "
            "(checkpoint_id IS NOT NULL AND checkpoint_ns IS NOT NULL "
            "AND root_checkpoint_id IS NOT NULL AND checkpoint_map IS NOT NULL "
            "AND checkpoint_map_digest IS NOT NULL AND checkpoint_next IS NOT NULL))",
            name="checkpoint_fields_consistent",
        ),
        CheckConstraint(
            "((phase = 'prepared' AND checkpoint_id IS NULL AND savepoint_id IS NULL) OR "
            "(phase = 'checkpointed' AND checkpoint_id IS NOT NULL "
            "AND savepoint_id IS NULL) OR "
            "(phase = 'linked' AND checkpoint_id IS NOT NULL "
            "AND savepoint_id IS NOT NULL))",
            name="phase_fields_consistent",
        ),
        Index(
            "ix_safe_point_publications_owner_run_phase",
            "owner_id",
            "run_id",
            "phase",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    stage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    safe_point_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    mcp_session_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    mcp_snapshot_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    artifact_version_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    writer_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    source_checkpoint_id: Mapped[str | None] = mapped_column(String(255))
    source_checkpoint_ns: Mapped[str | None] = mapped_column(String(1_024))
    checkpoint_as_node: Mapped[str | None] = mapped_column(String(255))
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    checkpoint_id: Mapped[str | None] = mapped_column(String(255))
    checkpoint_ns: Mapped[str | None] = mapped_column(String(1_024))
    root_checkpoint_id: Mapped[str | None] = mapped_column(String(255))
    checkpoint_map: Mapped[list[list[str]] | None] = mapped_column(_json_type())
    checkpoint_map_digest: Mapped[str | None] = mapped_column(String(64))
    checkpoint_next: Mapped[list[str] | None] = mapped_column(_json_type())
    savepoint_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    idempotency_key: Mapped[str] = mapped_column(String(IDEMPOTENCY_KEY_LENGTH), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class RunResumeOperationModel(Base, UpdatedAtMixin, TenantScopedMixin):
    """Durable control-plane intent and graph observation for one resumed Run."""

    __tablename__ = "run_resume_operations"
    __table_args__ = (
        _tenant_task_scope_fk("run_resume_operations"),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "run_id"],
            ["runs.owner_id", "runs.task_id", "runs.run_id"],
            name="fk_run_resume_operations_owner_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "savepoint_id"],
            [
                "checkpoint_links.owner_id",
                "checkpoint_links.task_id",
                "checkpoint_links.savepoint_id",
            ],
            name="fk_run_resume_operations_owner_task_savepoint",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_id", "task_id", "target_mcp_session_id"],
            [
                "mcp_sessions.owner_id",
                "mcp_sessions.task_id",
                "mcp_sessions.session_id",
            ],
            name="fk_run_resume_operations_owner_task_mcp_session",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("operation_id", name="uq_run_resume_operations_operation_id"),
        CheckConstraint(
            "phase IN ('intent', 'graph_inflight', 'graph_observed', 'finalized')",
            name="phase_valid",
        ),
        CheckConstraint(
            "observed_outcome IS NULL OR observed_outcome IN ('completed', 'interrupted')",
            name="observed_outcome_valid",
        ),
        CheckConstraint(
            "(observed_checkpoint_cursor IS NULL) = (observed_outcome IS NULL)",
            name="observation_pair",
        ),
        CheckConstraint(
            "((phase IN ('intent', 'graph_inflight')) AND observed_checkpoint_cursor IS NULL) "
            "OR ((phase IN ('graph_observed', 'finalized')) "
            "AND observed_checkpoint_cursor IS NOT NULL)",
            name="phase_observation_consistent",
        ),
        CheckConstraint(
            "source_checkpoint_cursor IS NOT NULL AND "
            "((phase IN ('intent', 'graph_inflight') "
            "AND observed_checkpoint_cursor IS NULL) OR "
            "(phase IN ('graph_observed', 'finalized') "
            "AND observed_checkpoint_cursor IS NOT NULL))",
            name="exact_cursor_phase_consistent",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        Index(
            "ix_run_resume_operations_owner_task_phase",
            "owner_id",
            "task_id",
            "phase",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(OWNER_ID_LENGTH), nullable=False)
    task_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    savepoint_id: Mapped[str] = mapped_column(String(ID_LENGTH), nullable=False)
    target_mcp_session_id: Mapped[str | None] = mapped_column(String(ID_LENGTH))
    command_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_checkpoint_cursor: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_checkpoint_cursor: Mapped[dict[str, Any] | None] = mapped_column(_json_type())
    observed_outcome: Mapped[str | None] = mapped_column(String(32))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


__all__ = [
    "ArtifactModel",
    "ArtifactVersionModel",
    "Base",
    "CheckpointLinkModel",
    "CheckpointProjectionReceiptModel",
    "FeedbackCommandModel",
    "UserSavepointCommandModel",
    "FeedbackDeliveryBatchMemberModel",
    "FeedbackDeliveryBatchModel",
    "GeometryResourceModel",
    "GeometryResourceVersionAssetModel",
    "GeometryResourceVersionModel",
    "ProductFamilyGeometryBindingModel",
    "ProductFamilyModel",
    "ProductFamilyVersionAssetModel",
    "ProductFamilyVersionModel",
    "IdentityMigrationQuarantineModel",
    "IFCRunProjectionModel",
    "IFCRunStageProjectionModel",
    "InterruptModel",
    "McpSessionModel",
    "McpSnapshotModel",
    "PrincipalModel",
    "RunEventModel",
    "RunModel",
    "RunResumeOperationModel",
    "RunStageBusinessViewModel",
    "SafePointPublicationModel",
    "StagePlanModel",
    "TaskTenantBindingModel",
    "TaskModel",
    "TenantModel",
    "TenantPrincipalModel",
    "UserModel",
]
