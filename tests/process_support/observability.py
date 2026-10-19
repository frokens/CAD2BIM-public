"""Independent assertions for public observability surfaces."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any


_LIFECYCLE_PREFIXES = ("llm.call", "tool.call", "structured_final")
_TERMINAL_SUFFIXES = ("completed", "failed")


def nearest_rank_percentile(samples: Sequence[float], percentile: float) -> float:
    if not samples:
        raise ValueError("percentile requires at least one sample")
    if percentile <= 0 or percentile > 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(float(sample) for sample in samples)
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return ordered[rank - 1]


def assert_public_event_evidence(
    events: Sequence[Mapping[str, Any]],
    *,
    max_event_bytes: int = 16 * 1024,
    modeler_tool_limit: int = 6,
) -> dict[str, Any]:
    """Validate the public API event stream without importing backend internals."""

    if not events:
        raise AssertionError("public event stream is empty")
    sequences = [int(event["sequence"]) for event in events]
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        raise AssertionError("public event sequence is not strictly increasing")
    event_ids = [str(event["event_id"]) for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise AssertionError("public event_id is not unique")

    lifecycle: dict[str, list[str]] = defaultdict(list)
    modeler_tools: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    modules: set[str] = set()
    linked_tool_items: set[str] = set()
    for event in events:
        rendered = json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(rendered) > max_event_bytes:
            raise AssertionError(f"public event exceeds {max_event_bytes} bytes")
        event_type = str(event["event_type"])
        item_id = str(event["item_id"])
        scope = event.get("scope") or {}
        modules.add(str(scope.get("module") or ""))
        prefix, _, suffix = event_type.rpartition(".")
        if prefix in _LIFECYCLE_PREFIXES:
            lifecycle[item_id].append(suffix)
        if event_type == "tool.call.started" and scope.get("agent") == "modeler":
            group = (
                scope.get("stage_key"),
                scope.get("attempt"),
                scope.get("model_turn"),
            )
            modeler_tools[group].add(item_id)
        if event_type.startswith("tool.call.") and event.get("subject_refs"):
            linked_tool_items.add(item_id)

    for item_id, transitions in lifecycle.items():
        counts = Counter(transitions)
        terminal_count = sum(counts[suffix] for suffix in _TERMINAL_SUFFIXES)
        if counts["started"] != 1 or terminal_count != 1 or transitions[0] != "started":
            raise AssertionError(
                f"lifecycle item {item_id} is not one started then one terminal: {transitions}"
            )
    for group, item_ids in modeler_tools.items():
        if len(item_ids) > modeler_tool_limit:
            raise AssertionError(
                f"Modeler tool limit exceeded for {group}: {len(item_ids)}"
            )

    return {
        "event_count": len(events),
        "first_sequence": sequences[0],
        "last_sequence": sequences[-1],
        "modules": sorted(module for module in modules if module),
        "lifecycle_item_count": len(lifecycle),
        "linked_tool_item_count": len(linked_tool_items),
        "modeler_tool_groups": {
            "|".join(str(part) for part in group): len(item_ids)
            for group, item_ids in sorted(modeler_tools.items(), key=lambda item: str(item[0]))
        },
    }


def sse_event_ids(payload: str) -> list[int]:
    return [
        int(line.removeprefix("id: "))
        for line in payload.splitlines()
        if line.startswith("id: ")
    ]


__all__ = [
    "assert_public_event_evidence",
    "nearest_rank_percentile",
    "sse_event_ids",
]
