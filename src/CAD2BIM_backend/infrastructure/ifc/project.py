"""Strict IfcOpenShell compiler for IFC Project business metadata."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from CAD2BIM_backend.domain.ifc_model import EncodedIfcRevision
from CAD2BIM_backend.domain.ifc_project import (
    IfcProjectBusinessError,
    IfcProjectCompilationError,
    IfcProjectIntegrityError,
    IfcProjectNoChangeError,
    IfcProjectSummary,
    UpdateIfcProjectCommand,
)
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec


_FIELD_TO_ATTRIBUTE = {
    "name": "Name",
    "description": "Description",
    "object_type": "ObjectType",
    "long_name": "LongName",
    "phase": "Phase",
}


@dataclass(frozen=True, slots=True)
class _ProtectedProjectState:
    global_id: str
    owner_history_id: int | None
    units_id: int | None
    context_ids: tuple[int, ...]
    other_entities: tuple[tuple[int, str], ...]


class IfcOpenShellProjectCompiler:
    """Apply only the five approved attributes on one private IFC copy."""

    def __init__(self, codec: IfcOpenShellModelCodec | None = None) -> None:
        self._codec = codec or IfcOpenShellModelCodec()

    def apply(
        self,
        base_content: bytes,
        command: UpdateIfcProjectCommand,
    ) -> EncodedIfcRevision:
        if not isinstance(command, UpdateIfcProjectCommand):
            raise TypeError("command must be UpdateIfcProjectCommand")

        private = self._codec.private_copy(base_content)
        model = private.model
        try:
            project = _require_project(model)
            before = _read_project(project)
            protected = _protected_state(model, project)
            expected = _expected_summary(before, command)
            if expected == before:
                raise IfcProjectNoChangeError(
                    "project update does not change Project business metadata"
                )

            for field_name in command.patch.model_fields_set:
                setattr(
                    project,
                    _FIELD_TO_ATTRIBUTE[field_name],
                    getattr(command.patch, field_name),
                )
            for field_name in command.clear_fields:
                setattr(project, _FIELD_TO_ATTRIBUTE[field_name], None)

            _assert_protected_state(model, project, protected)
            content = private.to_bytes()
            if content == base_content:
                raise IfcProjectNoChangeError(
                    "project update does not change the exact IFC revision"
                )

            # Candidate bytes are reopened independently before they can leave
            # the compiler boundary.  This validates both the requested values
            # and the protected Project/model state after serialization.
            candidate_private = self._codec.private_copy(content)
            candidate_model = candidate_private.model
            candidate_project = _require_project(candidate_model)
            if _read_project(candidate_project) != expected:
                raise IfcProjectCompilationError(
                    "saved IFC candidate does not contain the requested Project metadata"
                )
            _assert_protected_state(candidate_model, candidate_project, protected)

            inspection = self._codec.inspect(content)
            if inspection.project_global_id != expected.global_id:
                raise IfcProjectCompilationError("saved IFC candidate changed Project GlobalId")
            if inspection.project_name != expected.name:
                raise IfcProjectCompilationError("saved IFC candidate changed Project Name")
            return EncodedIfcRevision(content=content, inspection=inspection)
        except (IfcProjectBusinessError, IfcProjectIntegrityError):
            raise
        except IfcProjectCompilationError:
            raise
        except Exception as exc:
            raise IfcProjectCompilationError("typed Project update could not be compiled") from exc

    def inspect_project(self, content: bytes) -> IfcProjectSummary:
        try:
            model = self._codec.private_copy(content).model
            return _read_project(_require_project(model))
        except IfcProjectIntegrityError:
            raise
        except Exception as exc:
            raise IfcProjectIntegrityError("IFC Project could not be inspected") from exc


def _require_project(model: Any) -> Any:
    projects = tuple(model.by_type("IfcProject"))
    if len(projects) != 1:
        raise IfcProjectIntegrityError("IFC revision must contain exactly one IfcProject")
    return projects[0]


def _read_project(project: Any) -> IfcProjectSummary:
    return IfcProjectSummary(
        global_id=str(getattr(project, "GlobalId", "") or ""),
        name=_required_name(getattr(project, "Name", None)),
        description=_optional_text(project.Description, "Description", 1024),
        object_type=_optional_text(project.ObjectType, "ObjectType", 255),
        long_name=_optional_text(project.LongName, "LongName", 255),
        phase=_optional_text(project.Phase, "Phase", 255),
    )


def _required_name(value: object) -> str:
    if not isinstance(value, str):
        raise IfcProjectIntegrityError("IfcProject Name must be a string")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise IfcProjectIntegrityError("IfcProject Name must not contain control characters")
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        raise IfcProjectIntegrityError("IfcProject Name must contain 1 to 255 characters")
    return normalized


def _optional_text(value: object, attribute: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise IfcProjectIntegrityError(f"IfcProject {attribute} must be a string or null")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise IfcProjectIntegrityError(
            f"IfcProject {attribute} must not contain control characters"
        )
    normalized = value.strip()
    if len(normalized) > limit:
        raise IfcProjectIntegrityError(
            f"IfcProject {attribute} must contain at most {limit} characters"
        )
    return normalized


def _expected_summary(
    current: IfcProjectSummary,
    command: UpdateIfcProjectCommand,
) -> IfcProjectSummary:
    changes = {
        field_name: getattr(command.patch, field_name)
        for field_name in command.patch.model_fields_set
    }
    changes.update({field_name: None for field_name in command.clear_fields})
    return current.model_copy(update=changes)


def _entity_id(entity: Any | None) -> int | None:
    return None if entity is None else int(entity.id())


def _protected_state(model: Any, project: Any) -> _ProtectedProjectState:
    return _ProtectedProjectState(
        global_id=str(project.GlobalId),
        owner_history_id=_entity_id(project.OwnerHistory),
        units_id=_entity_id(project.UnitsInContext),
        context_ids=tuple(int(context.id()) for context in project.RepresentationContexts or ()),
        other_entities=tuple(
            (int(entity.id()), str(entity)) for entity in model if entity != project
        ),
    )


def _assert_protected_state(
    model: Any,
    project: Any,
    expected: _ProtectedProjectState,
) -> None:
    actual = _protected_state(model, project)
    if actual.global_id != expected.global_id:
        raise IfcProjectCompilationError("Project update changed GlobalId")
    if actual.owner_history_id != expected.owner_history_id:
        raise IfcProjectCompilationError("Project update changed OwnerHistory")
    if actual.units_id != expected.units_id:
        raise IfcProjectCompilationError("Project update changed UnitsInContext")
    if actual.context_ids != expected.context_ids:
        raise IfcProjectCompilationError("Project update changed RepresentationContexts")
    if actual.other_entities != expected.other_entities:
        raise IfcProjectCompilationError("Project update changed a non-Project IFC entity")


__all__ = ["IfcOpenShellProjectCompiler"]
