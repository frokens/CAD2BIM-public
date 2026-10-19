"""Finite State Resource projection from each field owner's exact checkpoint."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import ValidationError

from CAD2BIM_backend.domain.state_read_binding import (
    STATE_RESOURCE_FIELDS,
    ExactStateReadBinding,
    StateFieldSource,
)
from CAD2BIM_backend.ports.graph_runtime import GraphCheckpointState, GraphRuntimePort


class StateResourceNotFound(LookupError):
    """The named resource has not been created at this exact State position."""


class StateResourceBindingError(ValueError):
    """The saved State does not match the authorized exact read binding."""


@dataclass(frozen=True, slots=True)
class StateResourceEntry:
    uri: str
    name: str
    description: str
    mime_type: str


_RESOURCES: dict[str, StateResourceEntry] = {
    "requirements": StateResourceEntry(
        uri="state://requirements",
        name="requirements",
        description="Current saved requirement sources and rows, including server-issued row IDs.",
        mime_type="application/json",
    ),
    "clarifications": StateResourceEntry(
        uri="state://clarifications",
        name="clarifications",
        description="Current saved clarification rows, including server-issued row IDs.",
        mime_type="application/json",
    ),
    "requirement_md": StateResourceEntry(
        uri="state://requirement-md",
        name="requirement-md",
        description="Current saved requirement working Markdown.",
        mime_type="text/markdown",
    ),
}
_BY_URI = {entry.uri: field for field, entry in _RESOURCES.items()}


@dataclass(frozen=True, slots=True)
class StateResourceContent:
    entry: StateResourceEntry
    text: str


class StateResourceService:
    """Read only the current field owner; a child ``None`` never falls back to parent."""

    def __init__(self, graph_runtime: GraphRuntimePort) -> None:
        self._graph_runtime = graph_runtime

    async def list_resources(
        self, binding: ExactStateReadBinding
    ) -> tuple[StateResourceEntry, ...]:
        saved_by_source: dict[tuple[str, str], Mapping[str, Any]] = {}
        available: list[StateResourceEntry] = []
        for field in STATE_RESOURCE_FIELDS:
            source = binding.source_for(field)
            key = (source.checkpoint_ns, source.checkpoint_id)
            if key not in saved_by_source:
                saved_by_source[key] = await self._saved_values(binding, source)
            if self._exists(saved_by_source[key], field):
                available.append(_RESOURCES[field])
        return tuple(available)

    async def read_resource(self, binding: ExactStateReadBinding, uri: str) -> StateResourceContent:
        field = _BY_URI.get(uri)
        if field is None:
            raise StateResourceNotFound("unknown State Resource")
        values = await self._saved_values(binding, binding.source_for(field))
        if not self._exists(values, field):
            raise StateResourceNotFound("State Resource is absent at this checkpoint")
        value = values[field]
        if field == "requirement_md":
            if not isinstance(value, str):
                raise StateResourceBindingError("saved requirement Markdown is not text")
            return StateResourceContent(entry=_RESOURCES[field], text=value)
        # The State contract owns validation; Resource only serializes the saved value.
        from CAD2BIM_backend.domain.requirement_state import Clarification, Requirements

        try:
            if field == "requirements":
                projected: Any = Requirements.model_validate(value).model_dump(mode="json")
            else:
                if not isinstance(value, list):
                    raise StateResourceBindingError("saved clarifications are not a list")
                projected = [
                    Clarification.model_validate(item).model_dump(mode="json") for item in value
                ]
        except ValidationError as exc:
            raise StateResourceBindingError("saved State Resource violates its schema") from exc
        return StateResourceContent(
            entry=_RESOURCES[field],
            text=json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    async def _saved_values(
        self, binding: ExactStateReadBinding, source: StateFieldSource
    ) -> Mapping[str, Any]:
        cursor = binding.checkpoint_cursor
        state: GraphCheckpointState | None = await self._graph_runtime.get_checkpoint_state(
            thread_id=cursor.thread_id,
            checkpoint_id=source.checkpoint_id,
            checkpoint_ns=source.checkpoint_ns,
        )
        if state is None:
            raise StateResourceBindingError("exact State checkpoint does not exist")
        saved = state.checkpoint
        if (
            state.target_run_id != binding.run_id
            or saved.thread_id != binding.thread_id
            or saved.checkpoint_id != source.checkpoint_id
            or saved.checkpoint_ns != source.checkpoint_ns
            or saved.root_checkpoint_id != cursor.root_checkpoint_id
            or dict(saved.checkpoint_map).get("") != cursor.root_checkpoint_id
            or dict(saved.checkpoint_map).get(source.checkpoint_ns) != source.checkpoint_id
        ):
            raise StateResourceBindingError(
                "exact State checkpoint or protocol differs from binding"
            )
        if (
            source.checkpoint_ns == cursor.checkpoint_ns
            and source.checkpoint_id == cursor.checkpoint_id
            and (
                saved.checkpoint_map != cursor.checkpoint_map
                or saved.checkpoint_map_digest != cursor.checkpoint_digest
            )
        ):
            raise StateResourceBindingError("exact State checkpoint map differs from binding")
        if not isinstance(state.values, Mapping):
            raise StateResourceBindingError("saved State values are not an object")
        return state.values

    @staticmethod
    def _exists(values: Mapping[str, Any], field: str) -> bool:
        # Empty list and empty Markdown are formed resources; missing and None are not.
        return field in values and values[field] is not None


__all__ = [
    "StateResourceBindingError",
    "StateResourceContent",
    "StateResourceEntry",
    "StateResourceNotFound",
    "StateResourceService",
]
