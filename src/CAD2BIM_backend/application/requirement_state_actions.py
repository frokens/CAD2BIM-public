"""Pure, bounded Requirement actions over one saved canonical State."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence, cast

from CAD2BIM_backend.domain.identity import stable_internal_identity_id
from CAD2BIM_backend.domain.requirement_state import (
    ChangeClarifications,
    ChangeRequirementMarkdown,
    ChangeRequirementRows,
    ChangeSources,
    ClarificationDraft,
    InitializeRequirements,
    RequirementRowDraft,
    RequirementState,
    SavedRequirementCommand,
)


class RequirementActionError(ValueError):
    """A saved command cannot be applied to this State."""


class MarkdownEditor(Protocol):
    def replace(
        self,
        content: str,
        *,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> tuple[str, int]: ...


@dataclass(frozen=True, slots=True)
class AppliedRequirementAction:
    state: RequirementState
    updates: dict[str, Any]
    changed: bool
    created_item_ids: tuple[str, ...] = ()
    replacements: int = 0


class RequirementStateActions:
    """Compile one previously saved server command into one Graph State update."""

    def __init__(self, *, markdown_editor: MarkdownEditor) -> None:
        self._markdown_editor = markdown_editor

    def apply(
        self,
        state: RequirementState,
        command: SavedRequirementCommand,
        *,
        role_execution_id: str,
        owned_fields: Sequence[str],
    ) -> AppliedRequirementAction:
        if not role_execution_id or command.role_execution_id != role_execution_id:
            raise RequirementActionError("saved command has another role execution identity")
        allowed = set(owned_fields)
        if len(allowed) != len(owned_fields) or allowed - set(RequirementState.model_fields):
            raise RequirementActionError("invalid owned fields")
        target = _action_target(command)
        if target not in allowed:
            raise RequirementActionError("action target is not an owned field")
        fields = state.model_dump(mode="python")
        created: list[str] = []
        replacements = 0
        action = command.action
        if isinstance(action, InitializeRequirements):
            if fields["requirements"] is not None:
                raise RequirementActionError("requirements already exist")
            rows = []
            for index, row in enumerate(action.rows):
                item_id = _item_id("req_", command, index)
                rows.append({"id": item_id, **row.model_dump()})
                created.append(item_id)
            fields["requirements"] = {"sources": list(action.sources), "requirements": rows}
        elif isinstance(action, ChangeSources):
            content = _require_requirements(fields)
            sources = content["sources"]
            for change in action.changes:
                if change.operation == "add":
                    if change.value in sources:
                        raise RequirementActionError("source already exists")
                    sources.append(change.value)
                else:
                    if sources.count(change.value) != 1:
                        raise RequirementActionError("source target must match exactly once")
                    position = sources.index(change.value)
                    if change.operation == "delete":
                        sources.pop(position)
                    else:
                        replacement = cast(str, change.replacement)
                        if replacement != change.value and replacement in sources:
                            raise RequirementActionError("source replacement already exists")
                        sources[position] = replacement
        elif isinstance(action, ChangeRequirementRows):
            rows = _require_requirements(fields)["requirements"]
            for index, change in enumerate(action.changes):
                if change.operation == "add":
                    item_id = _item_id("req_", command, index)
                    row = {"id": item_id, **cast(RequirementRowDraft, change.row).model_dump()}
                    if change.after_id is None:
                        rows.append(row)
                    else:
                        rows.insert(_row_position(rows, change.after_id) + 1, row)
                    created.append(item_id)
                else:
                    row_id = cast(str, change.row_id)
                    position = _row_position(rows, row_id)
                    if change.operation == "delete":
                        rows.pop(position)
                    else:
                        rows[position] = {
                            "id": row_id,
                            **cast(RequirementRowDraft, change.row).model_dump(),
                        }
        elif isinstance(action, ChangeClarifications):
            items = fields["clarifications"]
            if items is None:
                items = []
                fields["clarifications"] = items
            for index, change in enumerate(action.changes):
                if change.operation == "add":
                    item_id = _item_id("clar_", command, index)
                    items.append({"id": item_id, **cast(ClarificationDraft, change.value).model_dump()})
                    created.append(item_id)
                else:
                    clarification_id = cast(str, change.clarification_id)
                    position = _row_position(items, clarification_id)
                    if change.operation == "delete":
                        items.pop(position)
                    else:
                        items[position] = {
                            "id": clarification_id,
                            **cast(ClarificationDraft, change.value).model_dump(),
                        }
        elif isinstance(action, ChangeRequirementMarkdown):
            content = fields["requirement_md"]
            for change in action.changes:
                if change.operation == "no_change":
                    continue
                if change.operation == "initialize":
                    if content is not None:
                        raise RequirementActionError("requirement_md already exists")
                    content = change.initial_content
                else:
                    if content is None:
                        raise RequirementActionError("requirement_md has not been initialized")
                    content, count = self._markdown_editor.replace(
                        content,
                        old_string=cast(str, change.old_string),
                        new_string=cast(str, change.new_string),
                        replace_all=change.replace_all,
                    )
                    replacements += count
            fields["requirement_md"] = content
        else:
            raise RequirementActionError("unsupported Requirement action")
        updated = RequirementState.model_validate(fields)
        changed = getattr(updated, target) != getattr(state, target)
        return AppliedRequirementAction(
            state=updated,
            updates=updated.model_dump(mode="json", include={target}) if changed else {},
            changed=changed,
            created_item_ids=tuple(created),
            replacements=replacements,
        )


def _action_target(command: SavedRequirementCommand) -> str:
    action = command.action
    if isinstance(action, (InitializeRequirements, ChangeSources, ChangeRequirementRows)):
        return "requirements"
    if isinstance(action, ChangeClarifications):
        return "clarifications"
    if isinstance(action, ChangeRequirementMarkdown):
        return "requirement_md"
    raise RequirementActionError("unsupported Requirement action")


def _require_requirements(fields: dict[str, Any]) -> dict[str, Any]:
    content = fields["requirements"]
    if content is None:
        raise RequirementActionError("requirements have not been initialized")
    return content


def _row_position(rows: list[dict[str, Any]], item_id: str) -> int:
    matches = [position for position, row in enumerate(rows) if row["id"] == item_id]
    if len(matches) != 1:
        raise RequirementActionError("item target must match exactly once")
    return matches[0]


def _item_id(prefix: str, command: SavedRequirementCommand, index: int) -> str:
    return stable_internal_identity_id(
        prefix, "requirement-state-item", command.role_execution_id, command.command_id, str(index)
    )


__all__ = [
    "AppliedRequirementAction",
    "MarkdownEditor",
    "RequirementActionError",
    "RequirementStateActions",
]
