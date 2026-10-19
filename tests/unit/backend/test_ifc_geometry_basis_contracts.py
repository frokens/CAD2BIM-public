from __future__ import annotations

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    CreateParentContextOperation,
    CreateSubcontextOperation,
    IfcGeometryBasisNoChangeError,
    IfcLengthUnit,
    IfcLocalCoordinateReferenceChange,
    IfcPlaneAngleUnit,
    IfcProjectUnitChange,
    IfcProjectedCrsCoordinateReferenceChange,
    IfcWorldCoordinateSystem2D,
    IfcWorldCoordinateSystem3D,
    UpdateParentContextOperation,
    UpdateSubcontextOperation,
)


def _model_wcs() -> IfcWorldCoordinateSystem3D:
    return IfcWorldCoordinateSystem3D(
        origin=(0.0, 0.0, 0.0),
        axis=(0.0, 0.0, 1.0),
        ref_direction=(1.0, 0.0, 0.0),
    )


def test_command_is_closed_and_requires_an_explicit_change() -> None:
    with pytest.raises(ValidationError, match="at least one change"):
        ConfigureIfcGeometryBasisCommand()

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ConfigureIfcGeometryBasisCommand.model_validate(
            {
                "coordinate_reference_change": {"mode": "local"},
                "rescale_existing_geometry": True,
            }
        )

    command = ConfigureIfcGeometryBasisCommand(
        coordinate_reference_change=IfcLocalCoordinateReferenceChange()
    )
    assert "reinterpret_existing_values" not in command.model_dump()
    assert IfcGeometryBasisNoChangeError.code == "IFC_NO_CHANGES"


def test_units_are_explicit_si_metre_and_radian_with_optional_prefix() -> None:
    change = IfcProjectUnitChange(
        length_unit=IfcLengthUnit(prefix="MILLI"),
        plane_angle_unit=IfcPlaneAngleUnit(prefix=None),
    )
    assert change.model_dump(mode="json") == {
        "length_unit": {"name": "METRE", "prefix": "MILLI"},
        "plane_angle_unit": {"name": "RADIAN", "prefix": None},
    }

    with pytest.raises(ValidationError):
        IfcLengthUnit.model_validate({"name": "FOOT", "prefix": None})
    with pytest.raises(ValidationError):
        IfcPlaneAngleUnit.model_validate({"name": "DEGREE", "prefix": None})
    with pytest.raises(ValidationError, match="at least one unit"):
        IfcProjectUnitChange()


def test_projected_crs_requires_explicit_epsg_and_complete_map_conversion() -> None:
    change = IfcProjectedCrsCoordinateReferenceChange(
        source_context_identifier=" Model ",
        crs_name="EPSG:4547",
        map_unit=IfcLengthUnit(),
        eastings=500000.0,
        northings=3400000.0,
        orthogonal_height=12.5,
        x_axis_abscissa=1.0,
        x_axis_ordinate=0.0,
        scale=1.0,
    )
    assert change.source_context_identifier == "Model"
    assert change.crs_name == "EPSG:4547"

    payload = change.model_dump(mode="json")
    for field in (
        "eastings",
        "northings",
        "orthogonal_height",
        "x_axis_abscissa",
        "x_axis_ordinate",
        "scale",
    ):
        missing = dict(payload)
        missing.pop(field)
        with pytest.raises(ValidationError):
            IfcProjectedCrsCoordinateReferenceChange.model_validate(missing)

    with pytest.raises(ValidationError, match="String should match pattern"):
        IfcProjectedCrsCoordinateReferenceChange.model_validate(
            {**payload, "crs_name": "WGS84 / guessed"}
        )
    with pytest.raises(ValidationError, match="non-zero"):
        IfcProjectedCrsCoordinateReferenceChange.model_validate(
            {**payload, "x_axis_abscissa": 0.0, "x_axis_ordinate": 0.0}
        )


def test_parent_context_dimensions_and_wcs_are_consistent() -> None:
    model = CreateParentContextOperation(
        context_identifier="Model",
        context_type="Model",
        coordinate_space_dimension=3,
        precision=1e-5,
        world_coordinate_system=_model_wcs(),
    )
    plan = CreateParentContextOperation(
        context_identifier="Plan",
        context_type="Plan",
        coordinate_space_dimension=2,
        precision=1e-5,
        world_coordinate_system=IfcWorldCoordinateSystem2D(
            origin=(0.0, 0.0), ref_direction=(1.0, 0.0)
        ),
    )
    assert model.action == "create_parent_context"
    assert plan.world_coordinate_system.kind == "2d"

    with pytest.raises(ValidationError, match="Model parent context"):
        CreateParentContextOperation.model_validate(
            {
                **model.model_dump(mode="json"),
                "coordinate_space_dimension": 2,
                "world_coordinate_system": plan.world_coordinate_system.model_dump(mode="json"),
            }
        )


def test_context_updates_do_not_accept_renaming_or_empty_changes() -> None:
    with pytest.raises(ValidationError, match="at least one changed field"):
        UpdateParentContextOperation(context_identifier="Model")
    with pytest.raises(ValidationError, match="at least one changed field"):
        UpdateSubcontextOperation(context_identifier="Body")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        UpdateSubcontextOperation.model_validate(
            {
                "action": "update_subcontext",
                "context_identifier": "Body",
                "new_context_identifier": "Body-2",
                "target_scale": 50.0,
            }
        )

    update = UpdateSubcontextOperation.model_validate(
        {
            "action": "update_subcontext",
            "context_identifier": "Body",
            "target_scale": None,
        }
    )
    assert "target_scale" in update.model_fields_set

    for field_name in (
        "context_type",
        "coordinate_space_dimension",
        "precision",
        "world_coordinate_system",
    ):
        with pytest.raises(ValidationError, match="cannot be null"):
            UpdateParentContextOperation.model_validate(
                {
                    "action": "update_parent_context",
                    "context_identifier": "Model",
                    field_name: None,
                }
            )

    for field_name in ("context_type", "parent_context_identifier", "target_view"):
        with pytest.raises(ValidationError, match="cannot be null"):
            UpdateSubcontextOperation.model_validate(
                {
                    "action": "update_subcontext",
                    "context_identifier": "Body",
                    field_name: None,
                }
            )


def test_subcontext_requires_explicit_parent_and_consistent_user_view() -> None:
    body = CreateSubcontextOperation(
        context_identifier="Body",
        context_type="Model",
        parent_context_identifier="Model",
        target_view="MODEL_VIEW",
    )
    assert body.parent_context_identifier == "Model"

    with pytest.raises(ValidationError, match="USERDEFINED"):
        CreateSubcontextOperation(
            context_identifier="Detail",
            context_type="Model",
            parent_context_identifier="Model",
            target_view="USERDEFINED",
        )
    with pytest.raises(ValidationError, match="USERDEFINED"):
        CreateSubcontextOperation(
            context_identifier="Body",
            context_type="Model",
            parent_context_identifier="Model",
            target_view="MODEL_VIEW",
            user_defined_target_view="Custom",
        )
