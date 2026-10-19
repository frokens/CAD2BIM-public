"""Pure role handoff and replace-only role/stage memory slots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict

from CAD2BIM_backend.domain.role_memory import RoleMemory

_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,127}$"


class RoleEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_slot: str
    child_state: dict[str, Any]


def role_memory_slot(*, role: str, stage_key: str | None) -> str:
    from re import fullmatch

    if fullmatch(_NAME_PATTERN, role) is None or (
        stage_key is not None and fullmatch(_NAME_PATTERN, stage_key) is None
    ):
        raise ValueError("invalid role or Stage key")
    return f"{role}:{stage_key or ''}"


def enter_role(
    parent_state: Mapping[str, Any],
    *,
    role: str,
    stage_key: str | None,
    business_entry_id: str,
    owned_fields: Sequence[str],
    business_schema: type[BaseModel] | None = None,
) -> RoleEntry:
    """Map saved parent fields into a new business entry, before child input saves."""

    if not business_entry_id.strip():
        raise ValueError("business entry identity is required")
    _require_owned_fields(owned_fields, business_schema)
    slot = role_memory_slot(role=role, stage_key=stage_key)
    memories = parent_state.get("role_memories", {})
    if not isinstance(memories, Mapping):
        raise ValueError("parent role memories are invalid")
    memory = RoleMemory.model_validate(memories.get(slot, {}))
    messages = deepcopy(memory.messages)
    child: dict[str, Any] = {}
    if owned_fields:
        assert business_schema is not None
        canonical = business_schema.model_validate({
            name: parent_state[name] for name in business_schema.model_fields if name in parent_state
        })
        canonical_values = canonical.model_dump(mode="json")
        child = {name: deepcopy(canonical_values[name]) for name in owned_fields}
    child.update(
        messages=messages,
        conversation_summary=memory.conversation_summary,
        tool_chain_summary=memory.tool_chain_summary,
        role=role,
        stage_key=stage_key,
        business_entry_id=business_entry_id,
    )
    return RoleEntry(memory_slot=slot, child_state=child)


def accept_role_output(
    parent_state: Mapping[str, Any],
    child_state: Mapping[str, Any],
    *,
    role: str,
    stage_key: str | None,
    owned_fields: Sequence[str],
    business_schema: type[BaseModel] | None = None,
) -> dict[str, Any]:
    """Return one parent update with owned fields and one replaced memory slot."""

    _require_owned_fields(owned_fields, business_schema)
    slot = role_memory_slot(role=role, stage_key=stage_key)
    if child_state.get("role") != role or child_state.get("stage_key") != stage_key:
        raise ValueError("child output role or Stage does not match the handoff")
    for name in owned_fields:
        if name not in child_state:
            raise ValueError(f"child output is missing owned field: {name}")
    next_parent: dict[str, Any] = {}
    if owned_fields:
        assert business_schema is not None
        canonical = business_schema.model_validate({
            name: child_state[name] for name in business_schema.model_fields if name in child_state
        })
        canonical_values = canonical.model_dump(mode="json")
        for name in owned_fields:
            next_parent[name] = deepcopy(canonical_values[name])
    memory = RoleMemory.model_validate(
        {name: child_state[name] for name in RoleMemory.model_fields if name in child_state}
    )
    memories = deepcopy(dict(parent_state.get("role_memories", {})))
    memories[slot] = memory.model_dump(mode="python")
    next_parent["role_memories"] = memories
    return next_parent


def _require_owned_fields(fields: Sequence[str], schema: type[BaseModel] | None) -> None:
    allowed = set(schema.model_fields) if schema is not None else set()
    if len(set(fields)) != len(fields) or set(fields) - allowed:
        raise ValueError("role handoff has invalid owned fields")


__all__ = [
    "RoleEntry",
    "RoleMemory",
    "accept_role_output",
    "enter_role",
    "role_memory_slot",
]
