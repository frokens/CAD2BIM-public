"""Project one business thread onto its exact active LangGraph checkpoint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from CAD2BIM_backend.ports.graph_runtime import (
    GraphCheckpointDescriptor,
    GraphExecutionResult,
)

from .checkpoint_lineage import describe_checkpoint_lineage


def project_execution_snapshot(
    root_snapshot: Any,
    *,
    thread_id: str,
) -> GraphExecutionResult:
    """Return the deepest active cursor while preserving parent state values.

    LangGraph keeps a parent checkpoint stable while a child subgraph advances.
    The child checkpoint is therefore the execution cursor at a nested
    interrupt.  Child snapshots contain only their state schema, so values are
    projected by overlaying every snapshot on the active root-to-leaf path.
    """

    path = _active_snapshot_path(root_snapshot)
    boundary = next(
        (snapshot for snapshot in reversed(path) if tuple(snapshot.interrupts or ())),
        path[-1],
    )
    descriptor = _descriptor(boundary, thread_id=thread_id)
    values = _merged_values(path)
    return GraphExecutionResult(
        values=values,
        checkpoint=descriptor,
        interrupts=_snapshot_interrupts(boundary),
    )


def project_checkpoint_cursor(
    root_snapshot: Any,
    cursor_snapshot: Any,
    *,
    thread_id: str,
) -> GraphExecutionResult:
    """Project an exact operation branch root and its exact cursor checkpoint."""

    root = _descriptor(root_snapshot, thread_id=thread_id)
    cursor = _descriptor(cursor_snapshot, thread_id=thread_id)
    if root.checkpoint_ns:
        raise RuntimeError("operation branch root must use the root checkpoint namespace")
    if cursor.checkpoint_ns:
        checkpoint_map = cursor_snapshot.config.get("configurable", {}).get("checkpoint_map", {})
        if str(checkpoint_map.get("") or "") != root.checkpoint_id:
            raise RuntimeError("operation cursor does not belong to its branch root")
        values = _merged_values((root_snapshot, cursor_snapshot))
    else:
        if cursor.checkpoint_id != root.checkpoint_id:
            raise RuntimeError("root operation cursor changed branch identity")
        values = root_snapshot.values
    return GraphExecutionResult(
        values=values,
        checkpoint=cursor,
        interrupts=_snapshot_interrupts(cursor_snapshot),
    )


def _active_snapshot_path(root_snapshot: Any) -> tuple[Any, ...]:
    if not _is_snapshot(root_snapshot) or not root_snapshot.config:
        raise RuntimeError("LangGraph execution produced no durable checkpoint")

    path = [root_snapshot]
    current = root_snapshot
    while True:
        active_children = [
            state for state in _child_snapshots(current) if _snapshot_is_active(state)
        ]
        if not active_children:
            return tuple(path)
        if len(active_children) != 1:
            raise RuntimeError("LangGraph execution has multiple active nested checkpoint branches")
        current = active_children[0]
        path.append(current)


def _child_snapshots(snapshot: Any) -> tuple[Any, ...]:
    result: list[Any] = []
    for task in tuple(getattr(snapshot, "tasks", ()) or ()):
        state = getattr(task, "state", None)
        if _is_snapshot(state):
            result.append(state)
    return tuple(result)


def _snapshot_is_active(snapshot: Any) -> bool:
    if tuple(snapshot.interrupts or ()) or tuple(snapshot.next or ()):
        return True
    return any(_snapshot_is_active(child) for child in _child_snapshots(snapshot))


def _is_snapshot(value: Any) -> bool:
    return all(
        hasattr(value, attribute)
        for attribute in ("config", "values", "next", "interrupts", "tasks")
    )


def _merged_values(path: tuple[Any, ...]) -> Any:
    if len(path) == 1:
        return path[0].values
    merged: dict[str, Any] = {}
    for snapshot in path:
        values = snapshot.values
        if not isinstance(values, Mapping):
            raise RuntimeError(
                "nested LangGraph state values must be objects for parent projection"
            )
        merged.update(values)
    return merged


def _descriptor(snapshot: Any, *, thread_id: str) -> GraphCheckpointDescriptor:
    configurable = snapshot.config.get("configurable", {})
    persisted_thread = str(configurable.get("thread_id") or "")
    checkpoint_id = str(configurable.get("checkpoint_id") or "")
    if persisted_thread != thread_id:
        raise RuntimeError("LangGraph returned a checkpoint outside the requested thread")
    if not checkpoint_id:
        raise RuntimeError("LangGraph checkpoint has no checkpoint_id")
    lineage = describe_checkpoint_lineage(configurable)
    return GraphCheckpointDescriptor(
        thread_id=thread_id,
        checkpoint_id=checkpoint_id,
        checkpoint_ns=str(configurable.get("checkpoint_ns") or ""),
        root_checkpoint_id=lineage.root_checkpoint_id,
        checkpoint_map=lineage.checkpoint_map,
        checkpoint_map_digest=lineage.checkpoint_map_digest,
        next_nodes=tuple(snapshot.next or ()),
        created_at=snapshot.created_at,
    )


def _snapshot_interrupts(snapshot: Any) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": str(getattr(item, "id", "")),
            "value": getattr(item, "value", None),
        }
        for item in tuple(snapshot.interrupts or ())
    )


__all__ = ["project_checkpoint_cursor", "project_execution_snapshot"]
