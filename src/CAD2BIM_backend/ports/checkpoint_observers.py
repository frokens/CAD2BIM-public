"""Module-neutral notification contract for committed LangGraph checkpoints."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class DurableCheckpointCommit(BaseModel):
    """Bounded exact state emitted only after the durable saver commits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_run_id: str = Field(min_length=1, max_length=80)
    thread_id: str = Field(min_length=1, max_length=255)
    checkpoint_id: str = Field(min_length=1, max_length=255)
    checkpoint_ns: str = Field(default="", max_length=1_024)
    root_checkpoint_id: str = Field(min_length=1, max_length=255)
    checkpoint_map: tuple[tuple[str, str], ...]
    checkpoint_map_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_checkpoint_id: str | None = Field(default=None, min_length=1, max_length=255)
    source: str | None = Field(default=None, max_length=64)
    step: int | None = None
    operation_id: str | None = Field(default=None, min_length=1, max_length=255)
    created_at: str | None = Field(default=None, max_length=100)
    values: dict[str, Any]
    updated_channels: tuple[str, ...] = ()


class DurableCheckpointObserver(Protocol):
    async def checkpoint_committed(self, commit: DurableCheckpointCommit) -> None: ...


__all__ = ["DurableCheckpointCommit", "DurableCheckpointObserver"]
