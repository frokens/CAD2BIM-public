from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict
from pydantic import ValidationError

from CAD2BIM_backend.infrastructure.agent_skills import AgentSkills
from CAD2BIM_backend.domain.requirement_state import RequirementState
from CAD2BIM_backend.application.role_state_handoff import (
    RoleMemory,
    accept_role_output,
    enter_role,
    role_memory_slot,
)


_OWNED = ("requirements", "clarifications", "previous_clarifications", "requirement_md")


def _parent():
    return {
        "requirements": {"sources": [], "requirements": []},
        "clarifications": [],
        "previous_clarifications": None,
        "requirement_md": "parent-old",
        "other_role_content": "untouched",
        "role_memories": {
            "requirement:": {
                "messages": [HumanMessage(id="old", content="old message").model_dump(mode="json")],
                "conversation_summary": "old summary",
                "tool_chain_summary": "old tools",
            }
        },
    }


def test_child_none_takes_ownership_and_parent_accepts_only_declared_fields() -> None:
    parent = _parent()
    entry = enter_role(
        parent,
        role="requirement",
        stage_key=None,
        business_entry_id="entry-1",
        owned_fields=_OWNED,
        business_schema=RequirementState,
    )
    assert entry.child_state["requirement_md"] == "parent-old"
    child = dict(entry.child_state)
    child["requirement_md"] = None
    child["other_role_content"] = "forged"
    child["messages"] = [HumanMessage(id="new", content="new message")]
    accepted = accept_role_output(
        parent, child, role="requirement", stage_key=None, owned_fields=_OWNED,
        business_schema=RequirementState,
    )
    assert accepted["requirement_md"] is None
    assert "other_role_content" not in accepted
    assert [message.content for message in RoleMemory.model_validate(
        accepted["role_memories"]["requirement:"]
    ).messages] == ["new message"]
    assert parent["requirement_md"] == "parent-old"
    with pytest.raises(ValueError, match="role or Stage"):
        accept_role_output(
            parent,
            {**child, "role": "planner"},
            role="requirement",
            stage_key=None,
            owned_fields=_OWNED,
            business_schema=RequirementState,
        )


def test_handoff_returns_only_owned_business_fields_and_memory_slot() -> None:
    parent = _parent()
    entry = enter_role(
        parent, role="requirement", stage_key=None,
        business_entry_id="entry-only", owned_fields=_OWNED,
        business_schema=RequirementState,
    )
    update = accept_role_output(
        parent, entry.child_state, role="requirement", stage_key=None, owned_fields=_OWNED,
        business_schema=RequirementState,
    )
    assert set(update) == {*_OWNED, "role_memories"}


@pytest.mark.asyncio
async def test_official_skill_read_survives_handoff_as_original_tool_message(tmp_path) -> None:
    package = tmp_path / "required"
    package.mkdir()
    path = package / "SKILL.md"
    header = "---\nname: required\ndescription: Use saved input identities.\n---\n"
    path.write_text(header + "Original instructions", encoding="utf-8")
    skills = AgentSkills(tmp_path)
    await skills.discover()
    call = {"name": "read_file", "args": {"file_path": "/required/SKILL.md"}, "id": "read-required"}
    result = await skills.aread(call, state={}, config={}, runtime=SimpleNamespace(context=None))
    assert isinstance(result, ToolMessage)
    parent = _parent()
    entry = enter_role(parent, role="requirement", stage_key=None,
        business_entry_id="entry-skill", owned_fields=_OWNED, business_schema=RequirementState)
    child = {**entry.child_state, "messages": [AIMessage(id="skill-call", content="", tool_calls=[call]), result]}
    accepted = accept_role_output(parent, child, role="requirement", stage_key=None,
        owned_fields=_OWNED, business_schema=RequirementState)
    path.write_text(header + "Current instructions", encoding="utf-8")
    restored = enter_role({**parent, **accepted}, role="requirement", stage_key=None,
        business_entry_id="entry-restored", owned_fields=_OWNED, business_schema=RequirementState)
    saved = restored.child_state["messages"][-1]
    assert isinstance(saved, ToolMessage) and saved.tool_call_id == "read-required"
    assert "Original instructions" in saved.content and "Current instructions" not in saved.content
    reread = await skills.aread({**call, "id": "read-current"}, state=restored.child_state,
        config={}, runtime=SimpleNamespace(context=None))
    assert "Current instructions" in reread.content
    assert "loaded_skills" not in restored.child_state


def test_role_memory_normalizes_official_saved_messages_and_rejects_plain_text() -> None:
    original = HumanMessage(id="human-1", content="Saved feedback")
    memory = RoleMemory.model_validate({
        "messages": [original.model_dump(mode="json"), message_to_dict(original)]
    })
    assert all(isinstance(message, HumanMessage) for message in memory.messages)
    assert [message.content for message in memory.messages] == ["Saved feedback"] * 2
    with pytest.raises(ValidationError, match="LangChain message"):
        RoleMemory.model_validate({"messages": ["plain text"]})


def test_tool_call_and_tool_result_survive_role_handoff_and_saved_reload() -> None:
    parent = _parent()
    entry = enter_role(
        parent, role="requirement", stage_key=None,
        business_entry_id="entry-tools", owned_fields=_OWNED,
        business_schema=RequirementState,
    )
    child = dict(entry.child_state)
    child["messages"] = [
        AIMessage(
            id="ai-tools", content="", tool_calls=[
                {"name": "read_source", "args": {"uri": "cad://source"}, "id": "call-1"}
            ],
        ),
        ToolMessage(id="tool-result", content="source body", tool_call_id="call-1"),
    ]
    accepted = accept_role_output(
        parent, child, role="requirement", stage_key=None, owned_fields=_OWNED,
        business_schema=RequirementState,
    )
    saved = accepted["role_memories"]["requirement:"]
    assert saved["messages"][0]["tool_calls"][0]["id"] == "call-1"
    assert saved["messages"][1]["tool_call_id"] == "call-1"
    memory = RoleMemory.model_validate(saved)
    assert isinstance(memory.messages[0], AIMessage)
    assert memory.messages[0].tool_calls[0]["args"] == {"uri": "cad://source"}
    assert isinstance(memory.messages[1], ToolMessage)
    assert memory.messages[1].tool_call_id == "call-1"
    reentry = enter_role(
        {**parent, **accepted}, role="requirement", stage_key=None,
        business_entry_id="entry-tools-again", owned_fields=_OWNED,
        business_schema=RequirementState,
    )
    assert isinstance(reentry.child_state["messages"][0], AIMessage)
    assert reentry.child_state["messages"][0].tool_calls[0]["id"] == "call-1"
    assert isinstance(reentry.child_state["messages"][1], ToolMessage)
    assert reentry.child_state["messages"][1].tool_call_id == "call-1"


def test_business_reentry_preserves_results_and_isolates_role_and_stage() -> None:
    parent = _parent()
    parent["gate_reentry_reason"] = "apply_default_clarifications"
    entry = enter_role(
        parent,
        role="requirement",
        stage_key=None,
        business_entry_id="entry-2",
        owned_fields=_OWNED,
        business_schema=RequirementState,
    )
    assert [message.content for message in entry.child_state["messages"]] == ["old message"]
    assert entry.child_state["clarifications"] == []
    assert entry.child_state["previous_clarifications"] is None
    assert "gate_reentry_reason" not in entry.child_state
    assert role_memory_slot(role="requirement", stage_key=None) != role_memory_slot(
        role="requirement", stage_key="stage_a"
    )
    assert role_memory_slot(role="requirement", stage_key="stage_a") != role_memory_slot(
        role="planner", stage_key="stage_a"
    )
    with pytest.raises(ValueError, match="missing owned"):
        accept_role_output(
            parent,
            {"role": "requirement", "stage_key": None, "messages": []},
            role="requirement",
            stage_key=None,
            owned_fields=_OWNED,
            business_schema=RequirementState,
        )


def test_handoff_business_values_are_plain_checkpoint_data() -> None:
    parent = _parent()
    parent["requirements"] = {
        "sources": ["drawing.md"],
        "requirements": [
            {
                "id": "req-one",
                "purpose": "pipe",
                "ifc_structure": "IfcPipeSegment",
                "modeling_method": "axis",
                "tools": ["CAD"],
            }
        ],
    }
    parent["clarifications"] = [
        {
            "id": "clar-one",
            "topic": "size",
            "question": "Which size?",
            "defaultAnswer": "unknown",
            "notes": "",
        }
    ]
    parent["requirement_md"] = ""
    entry = enter_role(
        parent,
        role="requirement",
        stage_key=None,
        business_entry_id="entry-plain",
        owned_fields=_OWNED,
        business_schema=RequirementState,
    )
    child = entry.child_state
    assert isinstance(child["requirements"], dict)
    assert isinstance(child["requirements"]["requirements"][0], dict)
    assert isinstance(child["clarifications"][0], dict)
    assert child["previous_clarifications"] is None
    assert child["requirement_md"] == ""
    json.dumps({name: child[name] for name in _OWNED})

    accepted = accept_role_output(
        parent, child, role="requirement", stage_key=None, owned_fields=_OWNED,
        business_schema=RequirementState,
    )
    assert isinstance(accepted["requirements"], dict)
    assert isinstance(accepted["clarifications"][0], dict)
    json.dumps({name: accepted[name] for name in _OWNED})


def test_read_only_role_replaces_memory_without_copying_parent_business_values() -> None:
    parent = _parent()
    entry = enter_role(
        parent,
        role="observer",
        stage_key="stage_a",
        business_entry_id="observe-one",
        owned_fields=(),
    )
    assert not (_OWNED and set(_OWNED) & set(entry.child_state))
    assert entry.child_state["messages"] == []
    child = {
        **entry.child_state,
        "messages": [HumanMessage(id="observation", content="observation")],
        "requirement_md": "forged",
    }
    accepted = accept_role_output(
        parent, child, role="observer", stage_key="stage_a", owned_fields=()
    )
    assert not set(_OWNED) & set(accepted)
    assert [message.content for message in RoleMemory.model_validate(
        accepted["role_memories"]["observer:stage_a"]
    ).messages] == ["observation"]
    assert [message.content for message in RoleMemory.model_validate(
        accepted["role_memories"]["requirement:"]
    ).messages] == ["old message"]


def test_owned_fields_require_explicit_business_schema() -> None:
    with pytest.raises(ValueError, match="invalid owned fields"):
        enter_role(
            _parent(), role="requirement", stage_key=None,
            business_entry_id="entry-missing-schema", owned_fields=_OWNED,
        )
    with pytest.raises(ValueError, match="invalid owned fields"):
        enter_role(
            _parent(), role="requirement", stage_key=None,
            business_entry_id="entry-invalid-field", owned_fields=("forged",),
            business_schema=RequirementState,
        )
