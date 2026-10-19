"""Canonical branch identity for an exact LangGraph checkpoint cursor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest


@dataclass(frozen=True, slots=True)
class CheckpointLineage:
    root_checkpoint_id: str
    checkpoint_map: tuple[tuple[str, str], ...]
    checkpoint_map_digest: str


def describe_checkpoint_lineage(configurable: Mapping[str, Any]) -> CheckpointLineage:
    checkpoint_id = str(configurable.get("checkpoint_id") or "").strip()
    checkpoint_ns = str(configurable.get("checkpoint_ns") or "")
    if not checkpoint_id:
        raise ValueError("checkpoint cursor has no checkpoint_id")

    raw_map = configurable.get("checkpoint_map")
    checkpoint_map = (
        {str(key): str(value) for key, value in raw_map.items() if str(value)}
        if isinstance(raw_map, Mapping)
        else {}
    )

    if checkpoint_ns:
        root_checkpoint_id = str(checkpoint_map.get("") or "")
        if not root_checkpoint_id:
            raise ValueError("nested checkpoint has no branch root")
        if checkpoint_map.get(checkpoint_ns) != checkpoint_id:
            raise ValueError("nested checkpoint does not match its branch map")
    else:
        mapped_root = str(checkpoint_map.get("") or "")
        if mapped_root and mapped_root != checkpoint_id:
            raise ValueError("root checkpoint branch root changed")
        root_checkpoint_id = checkpoint_id
        checkpoint_map.setdefault("", checkpoint_id)

    normalized_map = tuple(sorted(checkpoint_map.items()))
    return CheckpointLineage(
        root_checkpoint_id=root_checkpoint_id,
        checkpoint_map=normalized_map,
        checkpoint_map_digest=checkpoint_cursor_digest(
            checkpoint_id=checkpoint_id,
            checkpoint_ns=checkpoint_ns,
            checkpoint_map=normalized_map,
        ),
    )


__all__ = ["CheckpointLineage", "describe_checkpoint_lineage"]
