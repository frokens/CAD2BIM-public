from __future__ import annotations

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.ifc_project import (
    IfcProjectNoChangeError,
    IfcProjectPatch,
    UpdateIfcProjectCommand,
)


def test_project_patch_is_closed_and_limited_to_five_business_fields() -> None:
    patch = IfcProjectPatch(
        name="  Gas Network  ",
        description="  Authoritative network model  ",
        object_type="  Distribution Project  ",
        long_name="  City Gas Distribution Network  ",
        phase="  As Built  ",
    )

    assert patch.model_dump(exclude_unset=True) == {
        "name": "Gas Network",
        "description": "Authoritative network model",
        "object_type": "Distribution Project",
        "long_name": "City Gas Distribution Network",
        "phase": "As Built",
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IfcProjectPatch.model_validate({"global_id": "0" * 22})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        UpdateIfcProjectCommand.model_validate(
            {"patch": {"units_in_context": "millimetre"}}
        )


def test_name_is_nonempty_and_cannot_be_cleared() -> None:
    for value in ("", "   ", "bad\nname"):
        with pytest.raises(ValidationError):
            IfcProjectPatch(name=value)

    with pytest.raises(ValidationError):
        UpdateIfcProjectCommand.model_validate(
            {"patch": {}, "clear_fields": ["name"]}
        )


def test_patch_nulls_are_rejected_in_favor_of_explicit_clear_fields() -> None:
    with pytest.raises(ValidationError, match="use clear_fields"):
        IfcProjectPatch(description=None)

    command = UpdateIfcProjectCommand(
        clear_fields=("description", "object_type", "long_name", "phase")
    )
    assert command.clear_fields == (
        "description",
        "object_type",
        "long_name",
        "phase",
    )


def test_command_requires_one_unambiguous_change() -> None:
    with pytest.raises(ValidationError, match="at least one change"):
        UpdateIfcProjectCommand()
    with pytest.raises(ValidationError, match="patched and cleared"):
        UpdateIfcProjectCommand(
            patch=IfcProjectPatch(description="new"),
            clear_fields=("description",),
        )
    with pytest.raises(ValidationError, match="duplicates"):
        UpdateIfcProjectCommand(clear_fields=("phase", "phase"))

    assert IfcProjectNoChangeError.code == "IFC_NO_CHANGES"


def test_operation_payload_preserves_only_submitted_patch_fields() -> None:
    command = UpdateIfcProjectCommand(
        patch=IfcProjectPatch(name="Network", phase="Design"),
        clear_fields=("description",),
    )
    assert command.operation_request_payload() == {
        "patch": {"name": "Network", "phase": "Design"},
        "clear_fields": ["description"],
    }
