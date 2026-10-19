"""Durable receipt for publishing one MCP/Graph safe point exactly once."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# One complete IFC generation can retain 34 sibling Stage subgraph cursors.
# Keep headroom for Requirement/CAD/workflow ancestry while bounding payload size.
MAX_CHECKPOINT_MAP_ENTRIES = 64


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SafePointPublicationPhase(StrEnum):
    PREPARED = "prepared"
    CHECKPOINTED = "checkpointed"
    LINKED = "linked"


class LinkedCursorScope(FrozenModel):
    """Tenant/run scope for deterministic linked safe-point selection."""

    owner_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    stage_key: str | None = Field(default=None, min_length=1, max_length=255)
    safe_point_kind: str | None = Field(default=None, min_length=1, max_length=64)


class PrepareSafePointPublicationRequest(FrozenModel):
    """Immutable Snapshot and Artifact identity prepared before a Graph checkpoint."""

    operation_id: str = Field(min_length=1, max_length=255)
    thread_id: str = Field(min_length=1, max_length=255)
    stage_key: str = Field(min_length=1, max_length=255)
    safe_point_kind: str = Field(min_length=1, max_length=64)
    mcp_session_id: str = Field(min_length=1, max_length=80)
    mcp_snapshot_id: str = Field(min_length=1, max_length=80)
    artifact_id: str = Field(min_length=1, max_length=80)
    artifact_version_id: str = Field(min_length=1, max_length=80)
    checksum: str
    writer_generation: int = Field(ge=1, strict=True)
    source_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_checkpoint_ns: str | None = Field(default=None, max_length=1_024)
    checkpoint_as_node: str | None = Field(default=None, min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)

    @field_validator("checksum")
    @classmethod
    def validate_checksum(cls, value: str) -> str:
        return _require_sha256(value, "checksum")

    @model_validator(mode="after")
    def validate_source_cursor(self) -> "PrepareSafePointPublicationRequest":
        _require_optional_source_cursor(
            source_checkpoint_id=self.source_checkpoint_id,
            source_checkpoint_ns=self.source_checkpoint_ns,
            checkpoint_as_node=self.checkpoint_as_node,
        )
        return self


class CheckpointSafePointPublicationRequest(FrozenModel):
    """Exact Graph cursor attached to a prepared publication."""

    operation_id: str = Field(min_length=1, max_length=255)
    writer_generation: int = Field(ge=1, strict=True)
    checkpoint_id: str = Field(min_length=1, max_length=255)
    checkpoint_ns: str = Field(default="", max_length=1_024)
    root_checkpoint_id: str = Field(min_length=1, max_length=255)
    checkpoint_map: tuple[tuple[str, str], ...] = Field(
        min_length=1,
        max_length=MAX_CHECKPOINT_MAP_ENTRIES,
    )
    checkpoint_map_digest: str
    checkpoint_next: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("checkpoint_map_digest")
    @classmethod
    def validate_checkpoint_map_digest(cls, value: str) -> str:
        return _require_sha256(value, "checkpoint_map_digest")

    @field_validator("checkpoint_map")
    @classmethod
    def normalize_checkpoint_map(
        cls, value: tuple[tuple[str, str], ...]
    ) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_checkpoint_lineage(self) -> "CheckpointSafePointPublicationRequest":
        _require_checkpoint_lineage(
            checkpoint_id=self.checkpoint_id,
            checkpoint_ns=self.checkpoint_ns,
            root_checkpoint_id=self.root_checkpoint_id,
            checkpoint_map=self.checkpoint_map,
            checkpoint_map_digest=self.checkpoint_map_digest,
        )
        return self


class LinkSafePointPublicationRequest(CheckpointSafePointPublicationRequest):
    """Exact Checkpoint Link attached after the cursor has been persisted."""

    savepoint_id: str = Field(min_length=1, max_length=80)


class SafePointPublicationRecord(FrozenModel):
    """Monotonic publication receipt: prepared -> checkpointed -> linked."""

    operation_id: str = Field(min_length=1, max_length=255)
    owner_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    thread_id: str = Field(min_length=1, max_length=255)
    stage_key: str = Field(min_length=1, max_length=255)
    safe_point_kind: str = Field(min_length=1, max_length=64)
    mcp_session_id: str = Field(min_length=1, max_length=80)
    mcp_snapshot_id: str = Field(min_length=1, max_length=80)
    artifact_id: str = Field(min_length=1, max_length=80)
    artifact_version_id: str = Field(min_length=1, max_length=80)
    checksum: str
    writer_generation: int = Field(ge=1, strict=True)
    source_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_checkpoint_ns: str | None = Field(default=None, max_length=1_024)
    checkpoint_as_node: str | None = Field(default=None, min_length=1, max_length=255)
    phase: SafePointPublicationPhase = SafePointPublicationPhase.PREPARED
    checkpoint_id: str | None = Field(default=None, min_length=1, max_length=255)
    checkpoint_ns: str | None = Field(default=None, max_length=1_024)
    root_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=255)
    checkpoint_map: tuple[tuple[str, str], ...] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CHECKPOINT_MAP_ENTRIES,
    )
    checkpoint_map_digest: str | None = None
    checkpoint_next: tuple[str, ...] | None = Field(default=None, max_length=32)
    savepoint_id: str | None = Field(default=None, min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=255)
    created_at: datetime
    updated_at: datetime

    @field_validator("checksum")
    @classmethod
    def validate_checksum(cls, value: str) -> str:
        return _require_sha256(value, "checksum")

    @field_validator("checkpoint_map_digest")
    @classmethod
    def validate_checkpoint_map_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_sha256(value, "checkpoint_map_digest")

    @field_validator("checkpoint_map")
    @classmethod
    def normalize_optional_checkpoint_map(
        cls, value: tuple[tuple[str, str], ...] | None
    ) -> tuple[tuple[str, str], ...] | None:
        return tuple(sorted(value)) if value is not None else None

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("publication timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_phase_attachments(self) -> "SafePointPublicationRecord":
        _require_optional_source_cursor(
            source_checkpoint_id=self.source_checkpoint_id,
            source_checkpoint_ns=self.source_checkpoint_ns,
            checkpoint_as_node=self.checkpoint_as_node,
        )
        checkpoint_attachments = (
            self.checkpoint_id,
            self.checkpoint_ns,
            self.root_checkpoint_id,
            self.checkpoint_map,
            self.checkpoint_map_digest,
            self.checkpoint_next,
        )
        has_all_checkpoint_attachments = all(item is not None for item in checkpoint_attachments)
        has_any_checkpoint_attachments = any(item is not None for item in checkpoint_attachments)
        if self.phase is SafePointPublicationPhase.PREPARED:
            if has_any_checkpoint_attachments or self.savepoint_id is not None:
                raise ValueError(
                    "prepared publication cannot have checkpoint attachments or a link"
                )
            return self
        if not has_all_checkpoint_attachments:
            raise ValueError("checkpointed publication requires all checkpoint attachments")
        _require_checkpoint_lineage(
            checkpoint_id=str(self.checkpoint_id),
            checkpoint_ns=str(self.checkpoint_ns),
            root_checkpoint_id=str(self.root_checkpoint_id),
            checkpoint_map=tuple(self.checkpoint_map or ()),
            checkpoint_map_digest=str(self.checkpoint_map_digest),
        )
        if self.phase is SafePointPublicationPhase.CHECKPOINTED:
            if self.savepoint_id is not None:
                raise ValueError("checkpointed publication cannot have savepoint_id")
            return self
        if self.savepoint_id is None:
            raise ValueError("linked publication requires savepoint_id")
        return self

    @property
    def id(self) -> str:
        return self.operation_id


def _require_sha256(value: str, field: str) -> str:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def checkpoint_cursor_digest(
    *,
    checkpoint_id: str,
    checkpoint_ns: str,
    checkpoint_map: tuple[tuple[str, str], ...],
) -> str:
    canonical_map = tuple(sorted(checkpoint_map))
    payload = json.dumps(
        {
            "checkpoint_id": checkpoint_id,
            "checkpoint_map": dict(canonical_map),
            "checkpoint_ns": checkpoint_ns,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_optional_source_cursor(
    *,
    source_checkpoint_id: str | None,
    source_checkpoint_ns: str | None,
    checkpoint_as_node: str | None,
) -> None:
    if source_checkpoint_id is None:
        if source_checkpoint_ns is not None or checkpoint_as_node is not None:
            raise ValueError("source checkpoint attachments require source_checkpoint_id")
        return
    if source_checkpoint_ns is None:
        raise ValueError("source checkpoint namespace is required")


def _require_checkpoint_lineage(
    *,
    checkpoint_id: str,
    checkpoint_ns: str,
    root_checkpoint_id: str,
    checkpoint_map: tuple[tuple[str, str], ...],
    checkpoint_map_digest: str,
) -> None:
    if any((key != "" and not key.strip()) or not value.strip() for key, value in checkpoint_map):
        raise ValueError("checkpoint_map contains a blank namespace or checkpoint")
    mapping = dict(checkpoint_map)
    if len(mapping) != len(checkpoint_map):
        raise ValueError("checkpoint_map namespaces must be unique")
    if mapping.get("") != root_checkpoint_id:
        raise ValueError("checkpoint_map does not match the branch root")
    if checkpoint_ns:
        if mapping.get(checkpoint_ns) != checkpoint_id:
            raise ValueError("checkpoint_map does not match the nested cursor")
    elif checkpoint_id != root_checkpoint_id:
        raise ValueError("root checkpoint cursor does not match the branch root")
    expected = checkpoint_cursor_digest(
        checkpoint_id=checkpoint_id,
        checkpoint_ns=checkpoint_ns,
        checkpoint_map=checkpoint_map,
    )
    if checkpoint_map_digest != expected:
        raise ValueError("checkpoint_map_digest does not match the cursor")


__all__ = [
    "CheckpointSafePointPublicationRequest",
    "LinkedCursorScope",
    "LinkSafePointPublicationRequest",
    "MAX_CHECKPOINT_MAP_ENTRIES",
    "PrepareSafePointPublicationRequest",
    "SafePointPublicationPhase",
    "SafePointPublicationRecord",
    "checkpoint_cursor_digest",
]
