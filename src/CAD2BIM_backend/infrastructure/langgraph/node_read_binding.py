"""Bind a running node to saved input checkpoints of its registered graph chain."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph._internal._config import recast_checkpoint_ns
from langgraph._internal._constants import NS_SEP

from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.control_plane import RunRecord, RunStatus
from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.domain.state_read_binding import (
    STATE_RESOURCE_FIELDS,
    ExactStateReadBinding,
    StateFieldSource,
    StateReadBindingError,
    StateResourceField,
)
from CAD2BIM_backend.ports.graph_runtime import GraphRuntimePort


async def resolve_node_read_binding(
    *,
    run: RunRecord,
    grant: RunnerCheckpointGrant,
    node_config: Mapping[str, Any],
    registered_graph_paths: tuple[str, ...],
    field_owner_paths: Mapping[StateResourceField, str],
    graph_runtime: GraphRuntimePort,
) -> ExactStateReadBinding:
    """Resolve root through current graph from one node's saved input map.

    ``registered_graph_paths`` is the trusted root-to-current chain returned
    by Graph registration, with the root represented by ``""``. No namespace
    component is inferred from an arbitrary model value or from task IDs.
    """

    if (
        run.status is not RunStatus.RUNNING
        or run.execution_protocol is None
        or grant.scope.run_id != run.run_id
        or grant.thread_id != run.thread_id
        or grant.writer_generation != run.writer_generation
        or grant.runner_id != run.claimed_by
        or grant.lease_token != run.lease_token
    ):
        raise StateReadBindingError("node has no current claimed Run")
    if not registered_graph_paths or registered_graph_paths[0] != "":
        raise StateReadBindingError("registered Graph hierarchy has no root")
    if len(set(registered_graph_paths)) != len(registered_graph_paths):
        raise StateReadBindingError("registered Graph hierarchy repeats a graph")
    for parent, child in zip(registered_graph_paths, registered_graph_paths[1:]):
        if not child or (parent and not child.startswith(parent + NS_SEP)):
            raise StateReadBindingError("registered Graph hierarchy is not ancestral")
    if set(field_owner_paths) != set(STATE_RESOURCE_FIELDS) or any(
        path not in registered_graph_paths for path in field_owner_paths.values()
    ):
        raise StateReadBindingError("Resource field owners must be registered Graph paths")

    configurable = node_config.get("configurable")
    if not isinstance(configurable, Mapping):
        raise StateReadBindingError("node has no saved input configuration")
    if configurable.get("thread_id") != run.thread_id:
        raise StateReadBindingError("node configuration belongs to another thread")
    node_ns = configurable.get("checkpoint_ns")
    raw_map = configurable.get("checkpoint_map")
    if not isinstance(node_ns, str) or not isinstance(raw_map, Mapping):
        raise StateReadBindingError("node has no saved input checkpoint map")
    current_path = registered_graph_paths[-1]
    recast_node_ns = recast_checkpoint_ns(node_ns)
    if not recast_node_ns or (
        current_path and not recast_node_ns.startswith(current_path + NS_SEP)
    ):
        raise StateReadBindingError("node is outside the registered current Graph")
    if any(not isinstance(ns, str) or not isinstance(cid, str) for ns, cid in raw_map.items()):
        raise StateReadBindingError("node checkpoint map contains invalid identities")
    input_map = dict(raw_map)
    if not input_map or any(not checkpoint_id for checkpoint_id in input_map.values()):
        raise StateReadBindingError("node checkpoint map contains an empty checkpoint")
    graph_ns_by_path: dict[str, str] = {}
    for graph_path in registered_graph_paths:
        # LangGraph keeps earlier sibling subgraph instances in the saved map
        # after a child is resumed. The trusted current node namespace selects
        # its own ancestor; historical siblings stay in the signed exact map.
        matching = [
            ns for ns in input_map
            if recast_checkpoint_ns(ns) == graph_path
            and (not ns or node_ns == ns or node_ns.startswith(ns + NS_SEP))
        ]
        if len(matching) != 1:
            raise StateReadBindingError("saved input map does not identify one registered Graph")
        graph_ns_by_path[graph_path] = matching[0]
    current_ns = graph_ns_by_path[current_path]
    if current_ns and not node_ns.startswith(current_ns + NS_SEP):
        raise StateReadBindingError("node namespace differs from its saved Graph input")
    if any(recast_checkpoint_ns(ns) not in registered_graph_paths for ns in input_map):
        raise StateReadBindingError("saved input map contains an unregistered Graph")

    for graph_path in registered_graph_paths:
        namespace = graph_ns_by_path[graph_path]
        checkpoint_id = input_map[namespace]
        saved = await graph_runtime.get_checkpoint_state(
            thread_id=run.thread_id, checkpoint_id=checkpoint_id, checkpoint_ns=namespace
        )
        if saved is None:
            raise StateReadBindingError("registered Graph input checkpoint is missing")
        descriptor = saved.checkpoint
        if (
            descriptor.thread_id != run.thread_id
            or descriptor.checkpoint_ns != namespace
            or descriptor.checkpoint_id != checkpoint_id
            or descriptor.root_checkpoint_id != input_map[""]
        ):
            raise StateReadBindingError("registered Graph input checkpoint contradicts Run")
    normalized_map = tuple(sorted(input_map.items()))
    try:
        cursor = CheckpointCursor(
            thread_id=run.thread_id,
            checkpoint_id=input_map[current_ns],
            checkpoint_ns=current_ns,
            root_checkpoint_id=input_map[""],
            checkpoint_map=normalized_map,
            checkpoint_digest=checkpoint_cursor_digest(
                checkpoint_id=input_map[current_ns],
                checkpoint_ns=current_ns,
                checkpoint_map=normalized_map,
            ),
        )
    except ValueError as exc:
        raise StateReadBindingError("node checkpoint lineage is malformed") from exc
    return ExactStateReadBinding(
        owner_id=run.owner_id,
        task_id=run.task_id,
        run_id=run.run_id,
        thread_id=run.thread_id,
        checkpoint_cursor=cursor,
        field_sources=tuple(
            StateFieldSource(
                field=field,
                checkpoint_ns=graph_ns_by_path[field_owner_paths[field]],
                checkpoint_id=input_map[graph_ns_by_path[field_owner_paths[field]]],
            )
            for field in STATE_RESOURCE_FIELDS
        ),
    )


__all__ = ["resolve_node_read_binding"]
