"""Closed Direct provider contracts for IFC Project metadata updates."""

from __future__ import annotations

import unicodedata
from typing import Any, Literal, TypeAlias

from pydantic import ConfigDict, Field, field_validator

from CAD2BIM_backend.domain.ifc_project import (
    IfcProjectPatch as DomainIfcProjectPatch,
)
from CAD2BIM_backend.domain.ifc_project import UpdateIfcProjectCommand
from CAD2BIM_mcp.direct.contract_base import DirectContract
from CAD2BIM_mcp.tool_docs import ifc_model as _tool_docs


IfcNullableProjectFieldInput: TypeAlias = Literal[
    "description",
    "object_type",
    "long_name",
    "phase",
]


def _remove_omission_defaults(schema: dict[str, Any]) -> None:
    """Keep omitted patch fields non-nullable without advertising null defaults."""

    for property_schema in schema.get("properties", {}).values():
        if property_schema.get("default") is None:
            property_schema.pop("default")


def _normalize_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("value must not contain control characters")
    return value.strip()


class IfcProjectPatchInput(DirectContract):
    __doc__ = _tool_docs.IFC_PROJECT_PATCH_INPUT_DESCRIPTION

    model_config = ConfigDict(json_schema_extra=_remove_omission_defaults)

    # ``None`` is an omission sentinel only. The non-optional annotations make explicit JSON
    # null invalid in both Pydantic validation and the Provider-visible schema.
    name: str = Field(
        default=None,
        min_length=1,
        max_length=255,
        description=_tool_docs.IFC_PROJECT_PATCH_INPUT_NAME_DESCRIPTION,
    )
    description: str = Field(
        default=None,
        max_length=1024,
        description=(_tool_docs.IFC_PROJECT_PATCH_INPUT_DESCRIPTION_DESCRIPTION),
    )
    object_type: str = Field(
        default=None,
        max_length=255,
        description=(_tool_docs.IFC_PROJECT_PATCH_INPUT_OBJECT_TYPE_DESCRIPTION),
    )
    long_name: str = Field(
        default=None,
        max_length=255,
        description=(_tool_docs.IFC_PROJECT_PATCH_INPUT_LONG_NAME_DESCRIPTION),
    )
    phase: str = Field(
        default=None,
        max_length=255,
        description=(_tool_docs.IFC_PROJECT_PATCH_INPUT_PHASE_DESCRIPTION),
    )

    @field_validator(
        "name",
        "description",
        "object_type",
        "long_name",
        "phase",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return _normalize_text(value)


class IfcProjectResult(DirectContract):
    global_id: str = Field(
        pattern=r"^[0-9A-Za-z_$]{22}$",
        description=_tool_docs.IFC_PROJECT_RESULT_GLOBAL_ID_DESCRIPTION,
    )
    name: str = Field(
        min_length=1,
        max_length=255,
        description=_tool_docs.IFC_PROJECT_RESULT_NAME_DESCRIPTION,
    )
    description: str | None = Field(
        max_length=1024,
        description=_tool_docs.IFC_PROJECT_RESULT_DESCRIPTION_DESCRIPTION,
    )
    object_type: str | None = Field(
        max_length=255,
        description=_tool_docs.IFC_PROJECT_RESULT_OBJECT_TYPE_DESCRIPTION,
    )
    long_name: str | None = Field(
        max_length=255,
        description=_tool_docs.IFC_PROJECT_RESULT_LONG_NAME_DESCRIPTION,
    )
    phase: str | None = Field(
        max_length=255,
        description=_tool_docs.IFC_PROJECT_RESULT_PHASE_DESCRIPTION,
    )


class IfcProjectUpdateResult(DirectContract):
    operation_alias: str = Field(
        pattern=r"^op_[0-9]{3,}$",
        max_length=32,
        description=_tool_docs.IFC_PROJECT_UPDATE_RESULT_OPERATION_ALIAS_DESCRIPTION,
    )
    project: IfcProjectResult = Field(
        description=_tool_docs.IFC_PROJECT_UPDATE_RESULT_PROJECT_DESCRIPTION
    )


def to_domain_project_update_command(
    patch: IfcProjectPatchInput,
    clear_fields: tuple[IfcNullableProjectFieldInput, ...],
) -> UpdateIfcProjectCommand:
    """Convert only explicitly submitted Provider fields into the domain command."""

    domain_patch = DomainIfcProjectPatch.model_validate(
        patch.model_dump(mode="python", exclude_unset=True)
    )
    return UpdateIfcProjectCommand(
        patch=domain_patch,
        clear_fields=clear_fields,
    )


def ifc_project_result(project: Any) -> IfcProjectResult:
    return IfcProjectResult(
        global_id=project.global_id,
        name=project.name,
        description=project.description,
        object_type=project.object_type,
        long_name=project.long_name,
        phase=project.phase,
    )


__all__ = [
    "IfcNullableProjectFieldInput",
    "IfcProjectPatchInput",
    "IfcProjectResult",
    "IfcProjectUpdateResult",
    "ifc_project_result",
    "to_domain_project_update_command",
]
