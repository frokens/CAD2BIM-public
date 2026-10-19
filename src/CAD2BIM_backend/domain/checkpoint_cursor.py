"""Shared exact LangGraph checkpoint cursor contract."""

from __future__ import annotations

import re
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from CAD2BIM_backend.domain.safe_point_publications import (
    MAX_CHECKPOINT_MAP_ENTRIES,
    checkpoint_cursor_digest,
)


_CURSOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
_CURSOR_NAMESPACE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9_.:|+\-]{0,1023})?$")
_SENSITIVE_CURSOR_TEXT = re.compile(
    r"(?i)(?:^|[_.:-])(?:api[_-]?key|authorization|password|secret|token)(?:$|[_.:-])"
)


class CheckpointCursor(BaseModel):
    """Exact durable branch cursor; never replace this with thread latest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str = Field(min_length=1, max_length=255)
    checkpoint_id: str = Field(min_length=1, max_length=255)
    checkpoint_ns: str = Field(default="", max_length=1_024)
    root_checkpoint_id: str = Field(min_length=1, max_length=255)
    checkpoint_map: tuple[tuple[str, str], ...] = Field(
        min_length=1,
        max_length=MAX_CHECKPOINT_MAP_ENTRIES,
    )
    checkpoint_digest: str = Field(min_length=64, max_length=64)

    @field_validator("thread_id", "checkpoint_id", "root_checkpoint_id")
    @classmethod
    def validate_cursor_id(cls, value: str) -> str:
        if _CURSOR_ID.fullmatch(value) is None or _SENSITIVE_CURSOR_TEXT.search(value):
            raise ValueError("checkpoint cursor must use a stable non-sensitive identifier")
        return value

    @field_validator("checkpoint_ns")
    @classmethod
    def validate_checkpoint_namespace(cls, value: str) -> str:
        if _CURSOR_NAMESPACE.fullmatch(value) is None or _SENSITIVE_CURSOR_TEXT.search(value):
            raise ValueError("checkpoint namespace must use a stable non-sensitive identifier")
        return value

    @field_validator("checkpoint_map")
    @classmethod
    def normalize_checkpoint_map(
        cls, value: tuple[tuple[str, str], ...]
    ) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_lineage(self) -> "CheckpointCursor":
        if any(
            len(namespace) > 1_024 or len(value) > 255 for namespace, value in self.checkpoint_map
        ):
            raise ValueError("checkpoint_map entry exceeds the cursor size limit")
        if any(
            (namespace != "" and not namespace.strip()) or not value.strip()
            for namespace, value in self.checkpoint_map
        ):
            raise ValueError("checkpoint_map contains a blank namespace or checkpoint")
        if any(
            _CURSOR_NAMESPACE.fullmatch(namespace) is None
            or _CURSOR_ID.fullmatch(checkpoint_id) is None
            or _SENSITIVE_CURSOR_TEXT.search(namespace)
            or _SENSITIVE_CURSOR_TEXT.search(checkpoint_id)
            for namespace, checkpoint_id in self.checkpoint_map
        ):
            raise ValueError("checkpoint_map contains an unsafe identifier")
        mapping = dict(self.checkpoint_map)
        if len(mapping) != len(self.checkpoint_map):
            raise ValueError("checkpoint_map namespaces must be unique")
        if mapping.get("") != self.root_checkpoint_id:
            raise ValueError("checkpoint_map does not match the branch root")
        if self.checkpoint_ns:
            if mapping.get(self.checkpoint_ns) != self.checkpoint_id:
                raise ValueError("checkpoint_map does not match the nested cursor")
        elif self.checkpoint_id != self.root_checkpoint_id:
            raise ValueError("root checkpoint cursor does not match the branch root")
        expected = checkpoint_cursor_digest(
            checkpoint_id=self.checkpoint_id,
            checkpoint_ns=self.checkpoint_ns,
            checkpoint_map=self.checkpoint_map,
        )
        if self.checkpoint_digest != expected:
            raise ValueError("checkpoint_digest does not match the cursor")
        return self


__all__ = ["CheckpointCursor"]
