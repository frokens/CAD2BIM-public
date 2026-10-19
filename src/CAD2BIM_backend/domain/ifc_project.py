"""Closed domain contracts for IFC Project business metadata."""

from __future__ import annotations

import unicodedata
from typing import ClassVar, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


IfcProjectField: TypeAlias = Literal[
    "name",
    "description",
    "object_type",
    "long_name",
    "phase",
]
IfcNullableProjectField: TypeAlias = Literal[
    "description",
    "object_type",
    "long_name",
    "phase",
]


class IfcProjectError(Exception):
    """Base failure for the IFC Project metadata capability."""

    code: ClassVar[str] = "IFC_PROJECT_ERROR"


class IfcProjectBusinessError(IfcProjectError, ValueError):
    """A deterministic Project command or model precondition failure."""


class IfcProjectNoChangeError(IfcProjectBusinessError):
    code = "IFC_NO_CHANGES"


class IfcProjectIntegrityError(IfcProjectError, RuntimeError):
    """Exact IFC bytes violate a server-owned Project invariant."""

    code = "IFC_MODEL_INTEGRITY_ERROR"


class IfcProjectCompilationError(IfcProjectError, RuntimeError):
    """A compiler or generated-candidate postcondition failed."""

    code = "IFC_PROJECT_COMPILATION_FAILED"


class IfcProjectContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _normalize_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("value must not contain control characters")
    return value.strip()


class IfcProjectPatch(IfcProjectContract):
    """Explicit non-null values for the five writable Project fields."""

    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1024)
    object_type: str | None = Field(default=None, max_length=255)
    long_name: str | None = Field(default=None, max_length=255)
    phase: str | None = Field(default=None, max_length=255)

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

    @model_validator(mode="after")
    def require_non_null_explicit_values(self) -> "IfcProjectPatch":
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"patch.{field_name} cannot be null; use clear_fields")
        if "name" in self.model_fields_set and not self.name:
            raise ValueError("patch.name must not be empty")
        return self


class UpdateIfcProjectCommand(IfcProjectContract):
    patch: IfcProjectPatch = Field(default_factory=IfcProjectPatch)
    clear_fields: tuple[IfcNullableProjectField, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def validate_change_set(self) -> "UpdateIfcProjectCommand":
        changed = set(self.patch.model_fields_set)
        cleared = set(self.clear_fields)
        if not changed and not cleared:
            raise ValueError("project update must contain at least one change")
        if len(cleared) != len(self.clear_fields):
            raise ValueError("clear_fields must not contain duplicates")
        overlap = changed & cleared
        if overlap:
            raise ValueError(
                f"fields cannot be patched and cleared together: {', '.join(sorted(overlap))}"
            )
        return self

    def operation_request_payload(self) -> dict[str, object]:
        return {
            "patch": self.patch.model_dump(mode="json", exclude_unset=True),
            "clear_fields": list(self.clear_fields),
        }


class IfcProjectSummary(IfcProjectContract):
    global_id: str = Field(min_length=22, max_length=22)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1024)
    object_type: str | None = Field(default=None, max_length=255)
    long_name: str | None = Field(default=None, max_length=255)
    phase: str | None = Field(default=None, max_length=255)


class IfcProjectUpdateResult(IfcProjectContract):
    """Public-safe result of one committed Project metadata update."""

    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$", max_length=32)
    project: IfcProjectSummary


__all__ = [
    "IfcNullableProjectField",
    "IfcProjectBusinessError",
    "IfcProjectCompilationError",
    "IfcProjectContract",
    "IfcProjectError",
    "IfcProjectField",
    "IfcProjectIntegrityError",
    "IfcProjectNoChangeError",
    "IfcProjectPatch",
    "IfcProjectSummary",
    "IfcProjectUpdateResult",
    "UpdateIfcProjectCommand",
]
