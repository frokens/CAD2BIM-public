from __future__ import annotations

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.application.requirement_state_actions import (
    RequirementActionError,
    RequirementStateActions,
)
from CAD2BIM_backend.domain.requirement_state import (
    ChangeClarifications,
    ChangeRequirementMarkdown,
    ChangeRequirementRows,
    ChangeSources,
    ClarificationChange,
    ClarificationDraft,
    InitializeRequirements,
    MarkdownChange,
    RequirementRowChange,
    RequirementRowDraft,
    RequirementState,
    SavedRequirementCommand,
    SourceChange,
)
from CAD2BIM_backend.infrastructure.markdown_editor import (
    DeepAgentsMarkdownEditor,
    MarkdownEditError,
)


def _command(number: int, action):
    return SavedRequirementCommand(
        command_id=f"command-{number}", role_execution_id="requirement-entry-1", action=action
    )


def _row(purpose: str) -> RequirementRowDraft:
    return RequirementRowDraft(
        purpose=purpose,
        ifc_structure="IfcPipeSegment",
        modeling_method="centerline",
        tools=["data_query"],
    )


def _apply(
    actions: RequirementStateActions, state: RequirementState, command: SavedRequirementCommand
):
    return actions.apply(
        state,
        command,
        role_execution_id="requirement-entry-1",
        owned_fields=("requirements", "clarifications", "requirement_md"),
    )


def test_requirement_rows_get_stable_server_ids_and_sources_stay_plain_strings() -> None:
    actions = RequirementStateActions(markdown_editor=DeepAgentsMarkdownEditor())
    first = _command(1, InitializeRequirements(sources=["drawing-A"], rows=[_row("Gas supply")]))
    initialized = _apply(actions, RequirementState(), first)
    row_id = initialized.state.requirements.requirements[0].id
    assert (
        row_id == _apply(actions, RequirementState(), first).state.requirements.requirements[0].id
    )
    assert initialized.state.requirements.sources == ["drawing-A"]
    assert initialized.state.requirements.requirements[0].purpose == "Gas supply"

    changed = _apply(
        actions,
        initialized.state,
        _command(
            2,
            ChangeRequirementRows(
                changes=[
                    RequirementRowChange(
                        operation="replace", row_id=row_id, row=_row("Distribution")
                    ),
                    RequirementRowChange(operation="add", after_id=row_id, row=_row("Metering")),
                ]
            ),
        ),
    )
    assert [row.purpose for row in changed.state.requirements.requirements] == [
        "Distribution",
        "Metering",
    ]
    assert changed.state.requirements.requirements[0].id == row_id
    assert changed.state.requirements.requirements[1].id != row_id

    with pytest.raises(RequirementActionError, match="source target"):
        _apply(
            actions,
            initialized.state,
            _command(
                3,
                ChangeSources(
                    changes=[
                        SourceChange(operation="add", value="drawing-B"),
                        SourceChange(operation="delete", value="missing"),
                    ]
                ),
            ),
        )
    assert initialized.state.requirements.sources == ["drawing-A"]


def test_clarification_none_empty_and_server_owned_id() -> None:
    actions = RequirementStateActions(markdown_editor=DeepAgentsMarkdownEditor())
    unassessed = RequirementState()
    assert unassessed.clarifications is None
    assessed = _apply(actions, unassessed, _command(1, ChangeClarifications())).state
    assert assessed.clarifications == []
    added = _apply(
        actions,
        assessed,
        _command(
            2,
            ChangeClarifications(
                changes=[
                    ClarificationChange(
                        operation="add",
                        value=ClarificationDraft(
                            topic="Pressure", question="Which class?", defaultAnswer="Low"
                        ),
                    )
                ]
            ),
        ),
    ).state
    clarification_id = added.clarifications[0].id
    assert clarification_id.startswith("clar_")
    replaced = _apply(
        actions,
        added,
        _command(
            3,
            ChangeClarifications(
                changes=[
                    ClarificationChange(
                        operation="replace",
                        clarification_id=clarification_id,
                        value=ClarificationDraft(
                            topic="Pressure", question="Confirmed class?", defaultAnswer="Low"
                        ),
                    )
                ]
            ),
        ),
    ).state
    assert replaced.clarifications[0].id == clarification_id
    assert replaced.clarifications[0].question == "Confirmed class?"


def test_markdown_creation_empty_vs_unformed_and_batch_rollback() -> None:
    actions = RequirementStateActions(markdown_editor=DeepAgentsMarkdownEditor())
    created_empty = _apply(
        actions,
        RequirementState(),
        _command(
            1,
            ChangeRequirementMarkdown(
                changes=[MarkdownChange(operation="initialize", initial_content="")]
            ),
        ),
    ).state
    assert created_empty.requirement_md == ""
    with pytest.raises(RequirementActionError, match="already exists"):
        _apply(
            actions,
            created_empty,
            _command(
                2,
                ChangeRequirementMarkdown(
                    changes=[MarkdownChange(operation="initialize", initial_content="overwrite")]
                ),
            ),
        )

    created = _apply(
        actions,
        RequirementState(),
        _command(
            3,
            ChangeRequirementMarkdown(
                changes=[MarkdownChange(operation="initialize", initial_content="alpha\nbeta\n")]
            ),
        ),
    ).state
    with pytest.raises(MarkdownEditError, match="not found"):
        _apply(
            actions,
            created,
            _command(
                4,
                ChangeRequirementMarkdown(
                    changes=[
                        MarkdownChange(
                            operation="replace_fragment", old_string="alpha", new_string="gamma"
                        ),
                        MarkdownChange(
                            operation="replace_fragment", old_string="missing", new_string="x"
                        ),
                    ]
                ),
            ),
        )
    assert created.requirement_md == "alpha\nbeta\n"
    applied = _apply(
        actions,
        created,
        _command(
            5,
            ChangeRequirementMarkdown(
                changes=[
                    MarkdownChange(
                        operation="replace_fragment", old_string="alpha", new_string="gamma"
                    )
                ]
            ),
        ),
    )
    assert applied.state.requirement_md == "gamma\nbeta\n"
    assert applied.replacements == 1


def test_checkpoint_parse_is_strict_on_owned_fields() -> None:
    state = RequirementState.from_checkpoint_values(
        {"requirements": {"sources": [], "requirements": []}, "messages": ["unrelated"]}
    )
    assert state.requirements.requirements == []
    with pytest.raises(ValidationError):
        RequirementState.from_checkpoint_values(
            {"requirements": {"sources": [], "requirements": [], "forged": True}}
        )


def test_static_action_field_combinations_fail_at_dto_boundary() -> None:
    with pytest.raises(ValidationError):
        SourceChange(operation="add", value="source", replacement="unexpected")
    with pytest.raises(ValidationError):
        RequirementRowChange(operation="add", row_id="req-old", row=_row("new"))
    with pytest.raises(ValidationError):
        ClarificationChange(operation="delete", clarification_id="clar-old", value=ClarificationDraft(
            topic="Pressure", question="Which?", defaultAnswer="Low"
        ))
    with pytest.raises(ValidationError):
        MarkdownChange(operation="initialize", initial_content="", old_string="stale")


def test_action_requires_current_role_identity_and_only_emits_owned_change() -> None:
    actions = RequirementStateActions(markdown_editor=DeepAgentsMarkdownEditor())
    state = RequirementState(requirement_md="parent-only")
    command = _command(99, ChangeClarifications())
    with pytest.raises(RequirementActionError, match="role execution"):
        actions.apply(
            state, command, role_execution_id="another-entry", owned_fields=("clarifications",)
        )
    with pytest.raises(RequirementActionError, match="owned"):
        actions.apply(
            state,
            _command(
                100,
                ChangeRequirementMarkdown(
                    changes=[
                        MarkdownChange(
                            operation="replace_fragment", old_string="parent", new_string="child"
                        )
                    ]
                ),
            ),
            role_execution_id="requirement-entry-1",
            owned_fields=("clarifications",),
        )
    applied = actions.apply(
        state,
        command,
        role_execution_id="requirement-entry-1",
        owned_fields=("clarifications",),
    )
    assert applied.updates == {"clarifications": []}
    assert applied.state.requirement_md == "parent-only"
    unchanged = actions.apply(
        state,
        _command(101, ChangeRequirementMarkdown(changes=[MarkdownChange(operation="no_change")])),
        role_execution_id="requirement-entry-1",
        owned_fields=("requirement_md",),
    )
    assert unchanged.changed is False
    assert unchanged.updates == {}
