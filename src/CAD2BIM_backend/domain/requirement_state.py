"""Canonical first-stage Requirement State and server-owned action contracts."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RequirementRowDraft(_ClosedModel):
    purpose: str
    ifc_structure: str
    modeling_method: str
    tools: list[str] = Field(default_factory=list)


class RequirementRow(RequirementRowDraft):
    """The single canonical row; id is server-owned targeting metadata."""

    id: str = Field(pattern=_ID_PATTERN)


class Requirements(_ClosedModel):
    sources: list[str] = Field(default_factory=list)
    requirements: list[RequirementRow] = Field(default_factory=list)


class ClarificationDraft(_ClosedModel):
    topic: str
    question: str
    defaultAnswer: str
    notes: str = ""


class Clarification(ClarificationDraft):
    id: str = Field(pattern=_ID_PATTERN)


class RequirementState(_ClosedModel):
    """Only these fields are the editable Requirement body in a checkpoint."""

    requirements: Requirements | None = None
    clarifications: list[Clarification] | None = None
    previous_clarifications: list[Clarification] | None = None
    requirement_md: str | None = None

    @classmethod
    def from_checkpoint_values(cls, values: Mapping[str, Any]) -> Self:
        """Validate the declared fields without interpreting unrelated Graph control data."""

        if not isinstance(values, Mapping):
            raise ValueError("checkpoint values must be a mapping")
        return cls.model_validate(
            {name: values[name] for name in cls.model_fields if name in values}
        )

    @model_validator(mode="after")
    def _unique_item_ids(self) -> Self:
        rows = self.requirements.requirements if self.requirements is not None else []
        if len({row.id for row in rows}) != len(rows):
            raise ValueError("Requirement row ids must be unique")
        for label, items in (
            ("clarification", self.clarifications),
            ("previous clarification", self.previous_clarifications),
        ):
            if items is not None and len({item.id for item in items}) != len(items):
                raise ValueError(f"{label} ids must be unique")
        return self


class InitializeRequirements(_ClosedModel):
    kind: Literal["initialize_requirements"] = "initialize_requirements"
    sources: list[str] = Field(default_factory=list)
    rows: list[RequirementRowDraft] = Field(default_factory=list)


class SourceChange(_ClosedModel):
    operation: Literal["add", "replace", "delete"]
    value: str
    replacement: str | None = None

    @model_validator(mode="after")
    def validate_operation_fields(self) -> Self:
        if not self.value:
            raise ValueError("source target must not be empty")
        if self.operation == "replace":
            if not self.replacement:
                raise ValueError("source replacement must not be empty")
        elif self.replacement is not None:
            raise ValueError("source add/delete cannot include replacement")
        return self


class ChangeSources(_ClosedModel):
    kind: Literal["change_sources"] = "change_sources"
    changes: list[SourceChange] = Field(min_length=1)


class RequirementRowChange(_ClosedModel):
    operation: Literal["add", "replace", "delete"]
    row_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    after_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    row: RequirementRowDraft | None = None

    @model_validator(mode="after")
    def validate_operation_fields(self) -> Self:
        if self.operation == "add":
            if self.row_id is not None or self.row is None:
                raise ValueError("requirement add needs a new row and no existing id")
        elif self.row_id is None or self.after_id is not None:
            raise ValueError("requirement change needs an existing id without after_id")
        elif (self.operation == "replace") != (self.row is not None):
            raise ValueError("requirement replace needs a row; delete forbids one")
        return self


class ChangeRequirementRows(_ClosedModel):
    kind: Literal["change_requirement_rows"] = "change_requirement_rows"
    changes: list[RequirementRowChange] = Field(min_length=1)


class ClarificationChange(_ClosedModel):
    operation: Literal["add", "replace", "delete"]
    clarification_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    value: ClarificationDraft | None = None

    @model_validator(mode="after")
    def validate_operation_fields(self) -> Self:
        if self.operation == "add":
            if self.clarification_id is not None or self.value is None:
                raise ValueError("clarification add needs a value and no existing id")
        elif self.clarification_id is None:
            raise ValueError("clarification change needs an existing id")
        elif (self.operation == "replace") != (self.value is not None):
            raise ValueError("clarification replace needs a value; delete forbids one")
        return self


class ChangeClarifications(_ClosedModel):
    kind: Literal["change_clarifications"] = "change_clarifications"
    assessed: Literal[True] = True
    changes: list[ClarificationChange] = Field(default_factory=list)


class MarkdownChange(_ClosedModel):
    operation: Literal["initialize", "replace_fragment", "no_change"]
    initial_content: str | None = None
    old_string: str | None = None
    new_string: str | None = None
    replace_all: bool = False

    @model_validator(mode="after")
    def validate_operation_fields(self) -> Self:
        if self.operation == "no_change":
            if any(
                value is not None
                for value in (self.initial_content, self.old_string, self.new_string)
            ) or self.replace_all:
                raise ValueError("no_change cannot include edit content")
        elif self.operation == "initialize":
            if (
                self.initial_content is None
                or self.old_string is not None
                or self.new_string is not None
                or self.replace_all
            ):
                raise ValueError("invalid markdown initialization")
        elif self.initial_content is not None or not self.old_string or self.new_string is None:
            raise ValueError("invalid markdown replacement")
        return self


class ChangeRequirementMarkdown(_ClosedModel):
    kind: Literal["change_requirement_md"] = "change_requirement_md"
    changes: list[MarkdownChange] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_edit_batch(self) -> Self:
        if len(self.changes) > 1 and any(
            change.operation == "no_change" for change in self.changes
        ):
            raise ValueError("no_change cannot be mixed with edits")
        return self


RequirementAction = Annotated[
    InitializeRequirements
    | ChangeSources
    | ChangeRequirementRows
    | ChangeClarifications
    | ChangeRequirementMarkdown,
    Field(discriminator="kind"),
]


class SavedRequirementCommand(_ClosedModel):
    """A server command already saved by the Graph before its execute node."""

    command_id: str = Field(pattern=_ID_PATTERN)
    role_execution_id: str = Field(pattern=_ID_PATTERN)
    action: RequirementAction


__all__ = [
    "ChangeClarifications",
    "ChangeRequirementMarkdown",
    "ChangeRequirementRows",
    "ChangeSources",
    "Clarification",
    "ClarificationChange",
    "ClarificationDraft",
    "InitializeRequirements",
    "MarkdownChange",
    "RequirementRow",
    "RequirementRowChange",
    "RequirementRowDraft",
    "RequirementState",
    "Requirements",
    "SavedRequirementCommand",
    "SourceChange",
]
