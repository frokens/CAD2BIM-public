from __future__ import annotations

from datetime import datetime, timezone

import pytest

from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    CreateParentContextOperation,
    CreateSubcontextOperation,
    IfcGeometryBasisIntegrityError,
    IfcGeometryBasisNoChangeError,
    IfcLengthUnit,
    IfcLocalCoordinateReferenceChange,
    IfcPlaneAngleUnit,
    IfcProjectUnitChange,
    IfcProjectedCrsCoordinateReferenceChange,
    IfcRepresentationContextAlreadyExistsError,
    IfcRepresentationContextIncompatibleError,
    IfcRepresentationContextNotFoundError,
    IfcWorldCoordinateSystem2D,
    IfcWorldCoordinateSystem3D,
    UpdateParentContextOperation,
    UpdateSubcontextOperation,
)
from CAD2BIM_backend.infrastructure.ifc.geometry_basis import (
    IfcOpenShellGeometryBasisCompiler,
)
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec


PROJECT_GLOBAL_ID = "1xvJu1wbjMywJMjRBpLw$1"
PROJECT_NAME = "Geometry Basis Test"
HEADER_TIMESTAMP = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)


def _codec() -> IfcOpenShellModelCodec:
    return IfcOpenShellModelCodec()


def _compiler() -> IfcOpenShellGeometryBasisCompiler:
    return IfcOpenShellGeometryBasisCompiler(_codec())


def _revision1() -> bytes:
    return (
        _codec()
        .create_revision1(
            project_global_id=PROJECT_GLOBAL_ID,
            project_name=PROJECT_NAME,
            header_timestamp=HEADER_TIMESTAMP,
        )
        .content
    )


def _model_wcs(
    *,
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
    ref_direction: tuple[float, float, float] = (1.0, 0.0, 0.0),
) -> IfcWorldCoordinateSystem3D:
    return IfcWorldCoordinateSystem3D(
        origin=origin,
        axis=(0.0, 0.0, 1.0),
        ref_direction=ref_direction,
    )


def _local_basis_command(
    *,
    prefix: str | None = "MILLI",
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> ConfigureIfcGeometryBasisCommand:
    return ConfigureIfcGeometryBasisCommand(
        project_unit_change=IfcProjectUnitChange(
            length_unit=IfcLengthUnit(prefix=prefix),
            plane_angle_unit=IfcPlaneAngleUnit(),
        ),
        coordinate_reference_change=IfcLocalCoordinateReferenceChange(),
        context_operations=(
            CreateParentContextOperation(
                context_identifier="Model",
                context_type="Model",
                coordinate_space_dimension=3,
                precision=1e-5,
                world_coordinate_system=_model_wcs(origin=origin),
                true_north=(0.0, 1.0),
            ),
            CreateSubcontextOperation(
                context_identifier="Body",
                context_type="Model",
                parent_context_identifier="Model",
                target_view="MODEL_VIEW",
            ),
        ),
    )


def _projected_change(
    *,
    map_unit_prefix: str | None = "MILLI",
) -> IfcProjectedCrsCoordinateReferenceChange:
    return IfcProjectedCrsCoordinateReferenceChange(
        source_context_identifier="Model",
        crs_name="EPSG:4547",
        description="CGCS2000 / 3-degree Gauss-Kruger CM 114E",
        geodetic_datum="China 2000",
        map_projection="Gauss-Kruger",
        map_zone="38",
        map_unit=IfcLengthUnit(prefix=map_unit_prefix),
        eastings=500000.0,
        northings=3400000.0,
        orthogonal_height=12.5,
        x_axis_abscissa=0.999847695,
        x_axis_ordinate=0.017452406,
        scale=0.9999,
    )


def test_two_non_equivalent_local_configurations_have_no_hidden_golden_default() -> None:
    compiler = _compiler()
    base = _revision1()

    millimetre = compiler.apply(base, _local_basis_command())
    metre_shifted = compiler.apply(
        base,
        _local_basis_command(prefix=None, origin=(125.0, -40.0, 7.5)),
    )

    assert millimetre.content != metre_shifted.content
    first = compiler.inspect_geometry_basis(millimetre.content)
    second = compiler.inspect_geometry_basis(metre_shifted.content)
    assert first is not None and second is not None
    assert first.length_unit.prefix == "MILLI"
    assert second.length_unit.prefix is None
    assert compiler.inspect_contexts(metre_shifted.content).contexts[
        1
    ].world_coordinate_system.origin == (
        125.0,
        -40.0,
        7.5,
    )


def test_partial_units_or_plan_context_remain_valid_but_unconfigured() -> None:
    compiler = _compiler()
    length_only = compiler.apply(
        _revision1(),
        ConfigureIfcGeometryBasisCommand(
            project_unit_change=IfcProjectUnitChange(length_unit=IfcLengthUnit())
        ),
    )
    assert compiler.inspect_geometry_basis(length_only.content) is None
    exact_inspection = _codec().inspect(length_only.content)
    assert exact_inspection.geometry_basis_configured is False
    assert exact_inspection.geometry_basis is None
    length_model = _codec().private_copy(length_only.content).model
    assert [str(unit.UnitType) for unit in length_model.by_type("IfcSIUnit")] == ["LENGTHUNIT"]

    units_only = compiler.apply(
        length_only.content,
        ConfigureIfcGeometryBasisCommand(
            project_unit_change=IfcProjectUnitChange(plane_angle_unit=IfcPlaneAngleUnit())
        ),
    )
    assert compiler.inspect_geometry_basis(units_only.content) is None

    plan_only = compiler.apply(
        _revision1(),
        ConfigureIfcGeometryBasisCommand(
            context_operations=(
                CreateParentContextOperation(
                    context_identifier="Plan",
                    context_type="Plan",
                    coordinate_space_dimension=2,
                    precision=1e-5,
                    world_coordinate_system=IfcWorldCoordinateSystem2D(
                        origin=(0.0, 0.0), ref_direction=(1.0, 0.0)
                    ),
                ),
            )
        ),
    )
    assert compiler.inspect_geometry_basis(plan_only.content) is None
    assert [
        item.context_identifier for item in compiler.inspect_contexts(plan_only.content).contexts
    ] == ["Plan"]


def test_compiler_creates_only_explicit_contexts_and_catalog_is_sorted() -> None:
    compiler = _compiler()
    encoded = compiler.apply(_revision1(), _local_basis_command())
    catalog = compiler.inspect_contexts(encoded.content)

    assert [item.context_identifier for item in catalog.contexts] == ["Body", "Model"]
    body, model = catalog.contexts
    assert body.kind == "subcontext"
    assert body.parent_context_identifier == "Model"
    assert body.target_view == "MODEL_VIEW"
    assert model.kind == "parent_context"
    assert model.precision == pytest.approx(1e-5)

    private = _codec().private_copy(encoded.content)
    identifiers = {
        context.ContextIdentifier
        for context in private.model.by_type("IfcGeometricRepresentationContext")
    }
    assert identifiers == {"Body", "Model"}
    assert not {"Axis", "Box", "FootPrint"} & identifiers


def test_duplicate_missing_parent_and_incompatible_context_fail_before_mutation() -> None:
    compiler = _compiler()
    base = compiler.apply(_revision1(), _local_basis_command()).content

    with pytest.raises(IfcRepresentationContextAlreadyExistsError):
        compiler.apply(
            base,
            ConfigureIfcGeometryBasisCommand(
                context_operations=(
                    CreateSubcontextOperation(
                        context_identifier="Body",
                        context_type="Model",
                        parent_context_identifier="Model",
                        target_view="MODEL_VIEW",
                    ),
                )
            ),
        )
    with pytest.raises(IfcRepresentationContextNotFoundError):
        compiler.apply(
            base,
            ConfigureIfcGeometryBasisCommand(
                context_operations=(
                    CreateSubcontextOperation(
                        context_identifier="Box",
                        context_type="Model",
                        parent_context_identifier="Missing",
                        target_view="MODEL_VIEW",
                    ),
                )
            ),
        )
    with pytest.raises(IfcRepresentationContextIncompatibleError):
        compiler.apply(
            base,
            ConfigureIfcGeometryBasisCommand(
                context_operations=(
                    CreateSubcontextOperation(
                        context_identifier="FootPrint",
                        context_type="Model",
                        parent_context_identifier="Model",
                        target_view="PLAN_VIEW",
                    ),
                )
            ),
        )

    assert (
        compiler.inspect_contexts(base).model_dump() == compiler.inspect_contexts(base).model_dump()
    )


def test_request_internal_duplicate_is_rejected_without_creating_contexts() -> None:
    compiler = _compiler()
    base = _revision1()
    command = ConfigureIfcGeometryBasisCommand(
        context_operations=(
            CreateParentContextOperation(
                context_identifier="Model",
                context_type="Model",
                coordinate_space_dimension=3,
                precision=1e-5,
                world_coordinate_system=_model_wcs(),
            ),
            CreateParentContextOperation(
                context_identifier="Model",
                context_type="Model",
                coordinate_space_dimension=3,
                precision=1e-6,
                world_coordinate_system=_model_wcs(),
            ),
        )
    )
    with pytest.raises(IfcRepresentationContextAlreadyExistsError):
        compiler.apply(base, command)
    assert compiler.inspect_contexts(base).contexts == ()


def test_context_graph_creation_does_not_depend_on_array_order() -> None:
    compiler = _compiler()
    encoded = compiler.apply(
        _revision1(),
        ConfigureIfcGeometryBasisCommand(
            context_operations=(
                CreateSubcontextOperation(
                    context_identifier="Body",
                    context_type="Model",
                    parent_context_identifier="Model",
                    target_view="MODEL_VIEW",
                ),
                CreateParentContextOperation(
                    context_identifier="Model",
                    context_type="Model",
                    coordinate_space_dimension=3,
                    precision=1e-5,
                    world_coordinate_system=_model_wcs(),
                ),
            )
        ),
    )

    catalog = compiler.inspect_contexts(encoded.content)
    assert [item.context_identifier for item in catalog.contexts] == ["Body", "Model"]
    assert catalog.contexts[0].parent_context_identifier == "Model"


def test_projected_crs_round_trips_exactly_then_local_explicitly_removes_it() -> None:
    compiler = _compiler()
    local = compiler.apply(_revision1(), _local_basis_command()).content
    projected = compiler.apply(
        local,
        ConfigureIfcGeometryBasisCommand(
            coordinate_reference_change=_projected_change(map_unit_prefix=None)
        ),
    )
    summary = compiler.inspect_geometry_basis(projected.content)
    assert summary is not None
    crs = summary.coordinate_reference
    assert crs.mode == "projected_crs"
    assert crs.source_context_identifier == "Model"
    assert crs.crs_name == "EPSG:4547"
    assert crs.eastings == 500000.0
    assert crs.northings == 3400000.0
    assert crs.scale == pytest.approx(0.9999)

    returned_local = compiler.apply(
        projected.content,
        ConfigureIfcGeometryBasisCommand(
            coordinate_reference_change=IfcLocalCoordinateReferenceChange()
        ),
    )
    local_summary = compiler.inspect_geometry_basis(returned_local.content)
    assert local_summary is not None
    assert local_summary.coordinate_reference.mode == "local"
    model = _codec().private_copy(returned_local.content).model
    assert not model.by_type("IfcCoordinateOperation")
    assert not model.by_type("IfcProjectedCRS")


def test_projected_crs_requires_explicit_existing_model_context() -> None:
    compiler = _compiler()
    with pytest.raises(IfcRepresentationContextNotFoundError):
        compiler.apply(
            _revision1(),
            ConfigureIfcGeometryBasisCommand(
                coordinate_reference_change=IfcProjectedCrsCoordinateReferenceChange(
                    source_context_identifier="Model",
                    crs_name="EPSG:4547",
                    eastings=0.0,
                    northings=0.0,
                    orthogonal_height=0.0,
                    x_axis_abscissa=1.0,
                    x_axis_ordinate=0.0,
                    scale=1.0,
                )
            ),
        )


def test_projected_map_unit_is_independent_of_optional_project_length_unit() -> None:
    compiler = _compiler()
    encoded = compiler.apply(
        _revision1(),
        ConfigureIfcGeometryBasisCommand(
            coordinate_reference_change=IfcProjectedCrsCoordinateReferenceChange(
                source_context_identifier="Model",
                crs_name="EPSG:4547",
                map_unit=IfcLengthUnit(prefix="MILLI"),
                eastings=0.0,
                northings=0.0,
                orthogonal_height=0.0,
                x_axis_abscissa=1.0,
                x_axis_ordinate=0.0,
                scale=1.0,
            ),
            context_operations=(
                CreateParentContextOperation(
                    context_identifier="Model",
                    context_type="Model",
                    coordinate_space_dimension=3,
                    precision=1e-5,
                    world_coordinate_system=_model_wcs(),
                ),
            ),
        ),
    )

    model = _codec().private_copy(encoded.content).model
    project = model.by_type("IfcProject")[0]
    projected = model.by_type("IfcProjectedCRS")[0]
    assert project.UnitsInContext is None
    assert str(projected.MapUnit.Name) == "METRE"
    assert str(projected.MapUnit.Prefix) == "MILLI"


def test_projected_map_unit_stays_independent_without_churn_or_orphans() -> None:
    compiler = _compiler()
    local = compiler.apply(_revision1(), _local_basis_command()).content
    command = ConfigureIfcGeometryBasisCommand(
        coordinate_reference_change=_projected_change()
    )
    projected = compiler.apply(local, command).content

    first_model = _codec().private_copy(projected).model
    project = first_model.by_type("IfcProject")[0]
    project_length = next(
        unit for unit in project.UnitsInContext.Units if str(unit.UnitType) == "LENGTHUNIT"
    )
    map_unit = first_model.by_type("IfcProjectedCRS")[0].MapUnit
    assert map_unit != project_length
    assert str(project_length.Prefix) == str(map_unit.Prefix) == "MILLI"

    with pytest.raises(IfcGeometryBasisNoChangeError):
        compiler.apply(projected, command)

    original_model = _codec().private_copy(projected).model
    original_conversion_id = original_model.by_type("IfcMapConversion")[0].id()
    original_target_id = original_model.by_type("IfcProjectedCRS")[0].id()
    original_map_unit_id = original_model.by_type("IfcProjectedCRS")[0].MapUnit.id()
    changed_projected = compiler.apply(
        projected,
        ConfigureIfcGeometryBasisCommand(
            coordinate_reference_change=_projected_change(map_unit_prefix="CENTI")
        ),
    ).content
    changed_model = _codec().private_copy(changed_projected).model
    assert [item.id() for item in changed_model.by_type("IfcMapConversion")] == [
        original_conversion_id
    ]
    assert [item.id() for item in changed_model.by_type("IfcProjectedCRS")] == [
        original_target_id
    ]
    assert changed_model.by_type("IfcProjectedCRS")[0].MapUnit.id() == original_map_unit_id
    assert len(changed_model.by_type("IfcSIUnit")) == 3

    reinterpreted = compiler.apply(
        changed_projected,
        ConfigureIfcGeometryBasisCommand(
            project_unit_change=IfcProjectUnitChange(length_unit=IfcLengthUnit())
        ),
    ).content
    reinterpreted_model = _codec().private_copy(reinterpreted).model
    project = reinterpreted_model.by_type("IfcProject")[0]
    project_length = next(
        unit for unit in project.UnitsInContext.Units if str(unit.UnitType) == "LENGTHUNIT"
    )
    map_unit = reinterpreted_model.by_type("IfcProjectedCRS")[0].MapUnit
    assert project_length.Prefix is None
    assert str(map_unit.Prefix) == "CENTI"

    returned_local = compiler.apply(
        reinterpreted,
        ConfigureIfcGeometryBasisCommand(
            coordinate_reference_change=IfcLocalCoordinateReferenceChange()
        ),
    ).content
    local_model = _codec().private_copy(returned_local).model
    assert not local_model.by_type("IfcCoordinateOperation")
    assert not local_model.by_type("IfcProjectedCRS")
    assert sorted(str(unit.UnitType) for unit in local_model.by_type("IfcSIUnit")) == [
        "LENGTHUNIT",
        "PLANEANGLEUNIT",
    ]


def test_existing_projected_crs_rejects_an_incompatible_source_context_update() -> None:
    compiler = _compiler()
    local = compiler.apply(_revision1(), _local_basis_command()).content
    projected = compiler.apply(
        local,
        ConfigureIfcGeometryBasisCommand(
            coordinate_reference_change=_projected_change()
        ),
    ).content

    with pytest.raises(IfcRepresentationContextIncompatibleError):
        compiler.apply(
            projected,
            ConfigureIfcGeometryBasisCommand(
                context_operations=(
                    UpdateParentContextOperation(
                        context_identifier="Model",
                        context_type="Plan",
                        coordinate_space_dimension=2,
                        world_coordinate_system=IfcWorldCoordinateSystem2D(
                            origin=(0.0, 0.0),
                            ref_direction=(1.0, 0.0),
                        ),
                    ),
                )
            ),
        )


def test_existing_projected_crs_survives_compatible_wcs_and_precision_updates() -> None:
    compiler = _compiler()
    local = compiler.apply(_revision1(), _local_basis_command()).content
    projected = compiler.apply(
        local,
        ConfigureIfcGeometryBasisCommand(
            coordinate_reference_change=_projected_change()
        ),
    ).content

    updated = compiler.apply(
        projected,
        ConfigureIfcGeometryBasisCommand(
            context_operations=(
                UpdateParentContextOperation(
                    context_identifier="Model",
                    precision=1e-7,
                    world_coordinate_system=_model_wcs(
                        origin=(12.0, 34.0, 5.0),
                        ref_direction=(0.0, 1.0, 0.0),
                    ),
                ),
            )
        ),
    )

    summary = compiler.inspect_geometry_basis(updated.content)
    assert summary is not None
    assert summary.precision == pytest.approx(1e-7)
    assert summary.coordinate_reference.mode == "projected_crs"
    assert summary.coordinate_reference.source_context_identifier == "Model"


def test_shared_project_and_projected_map_unit_is_rejected_as_integrity_drift() -> None:
    compiler = _compiler()
    local = compiler.apply(_revision1(), _local_basis_command()).content
    projected = compiler.apply(
        local,
        ConfigureIfcGeometryBasisCommand(
            coordinate_reference_change=_projected_change()
        ),
    ).content
    private = _codec().private_copy(projected)
    project = private.model.by_type("IfcProject")[0]
    project_length = next(
        unit for unit in project.UnitsInContext.Units if str(unit.UnitType) == "LENGTHUNIT"
    )
    target = private.model.by_type("IfcProjectedCRS")[0]
    old_map_unit = target.MapUnit
    target.MapUnit = project_length
    private.model.remove(old_map_unit)

    with pytest.raises(IfcGeometryBasisIntegrityError, match="independent"):
        compiler.inspect_geometry_basis(private.to_bytes())


def test_zero_projected_x_axis_is_rejected_during_exact_readback() -> None:
    compiler = _compiler()
    local = compiler.apply(_revision1(), _local_basis_command()).content
    projected = compiler.apply(
        local,
        ConfigureIfcGeometryBasisCommand(
            coordinate_reference_change=_projected_change()
        ),
    ).content
    private = _codec().private_copy(projected)
    conversion = private.model.by_type("IfcMapConversion")[0]
    conversion.XAxisAbscissa = 0.0
    conversion.XAxisOrdinate = 0.0

    with pytest.raises(IfcGeometryBasisIntegrityError):
        compiler.inspect_geometry_basis(private.to_bytes())


def test_context_update_preflight_rejects_invalid_post_state() -> None:
    compiler = _compiler()
    configured = compiler.apply(
        _revision1(),
        ConfigureIfcGeometryBasisCommand(
            context_operations=(
                CreateParentContextOperation(
                    context_identifier="Model",
                    context_type="Model",
                    coordinate_space_dimension=3,
                    precision=1e-5,
                    world_coordinate_system=_model_wcs(),
                ),
                CreateSubcontextOperation(
                    context_identifier="Custom",
                    context_type="Model",
                    parent_context_identifier="Model",
                    target_view="USERDEFINED",
                    user_defined_target_view="Coordination",
                ),
            )
        ),
    ).content

    invalid_commands = (
        ConfigureIfcGeometryBasisCommand(
            context_operations=(
                UpdateParentContextOperation(
                    context_identifier="Model",
                    world_coordinate_system=IfcWorldCoordinateSystem2D(
                        origin=(0.0, 0.0), ref_direction=(1.0, 0.0)
                    ),
                ),
            )
        ),
        ConfigureIfcGeometryBasisCommand(
            context_operations=(
                UpdateSubcontextOperation(context_identifier="Custom", target_view="MODEL_VIEW"),
            )
        ),
    )
    for command in invalid_commands:
        with pytest.raises(IfcRepresentationContextIncompatibleError):
            compiler.apply(configured, command)


def test_updates_reinterpret_values_without_replacing_context_entities_or_products() -> None:
    compiler = _compiler()
    configured = compiler.apply(_revision1(), _local_basis_command()).content
    private = _codec().private_copy(configured)
    model = private.model
    body = next(
        context
        for context in model.by_type("IfcGeometricRepresentationSubContext")
        if context.ContextIdentifier == "Body"
    )
    placement = model.create_entity(
        "IfcLocalPlacement",
        RelativePlacement=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=(1000.0, 2000.0, 3000.0)),
            Axis=model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
            RefDirection=model.create_entity("IfcDirection", DirectionRatios=(1.0, 0.0, 0.0)),
        ),
    )
    bounding_box = model.create_entity(
        "IfcBoundingBox",
        Corner=model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
        XDim=10.0,
        YDim=20.0,
        ZDim=30.0,
    )
    shape = model.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body,
        RepresentationIdentifier="Body",
        RepresentationType="BoundingBox",
        Items=(bounding_box,),
    )
    representation = model.create_entity("IfcProductDefinitionShape", Representations=(shape,))
    model.create_entity(
        "IfcBuildingElementProxy",
        GlobalId="0EEEEEEEEEEEEEEEEEEEEE",
        ObjectPlacement=placement,
        Representation=representation,
    )
    with_product = private.to_bytes()
    original_body_id = body.id()

    updated = compiler.apply(
        with_product,
        ConfigureIfcGeometryBasisCommand(
            project_unit_change=IfcProjectUnitChange(length_unit=IfcLengthUnit()),
            context_operations=(
                UpdateParentContextOperation(
                    context_identifier="Model",
                    precision=1e-7,
                    world_coordinate_system=_model_wcs(ref_direction=(0.0, 1.0, 0.0)),
                ),
                UpdateSubcontextOperation(context_identifier="Body", target_scale=50.0),
            ),
        ),
    )
    result = _codec().private_copy(updated.content).model
    product = result.by_type("IfcBuildingElementProxy")[0]
    coordinates = product.ObjectPlacement.RelativePlacement.Location.Coordinates
    result_body = next(
        context
        for context in result.by_type("IfcGeometricRepresentationSubContext")
        if context.ContextIdentifier == "Body"
    )

    assert tuple(coordinates) == (1000.0, 2000.0, 3000.0)
    assert product.Representation.Representations[0].Items[0].XDim == 10.0
    assert product.Representation.Representations[0].ContextOfItems == result_body
    assert result_body.id() == original_body_id
    assert result_body.TargetScale == 50.0
    summary = compiler.inspect_geometry_basis(updated.content)
    assert summary is not None
    assert summary.length_unit.prefix is None
    assert summary.precision == pytest.approx(1e-7)
    catalog = compiler.inspect_contexts(updated.content)
    assert (
        next(
            item for item in catalog.contexts if item.context_identifier == "Body"
        ).representation_count
        == 1
    )


def test_semantic_no_change_does_not_emit_candidate_revision() -> None:
    compiler = _compiler()
    configured = compiler.apply(_revision1(), _local_basis_command()).content
    no_change_commands = (
        ConfigureIfcGeometryBasisCommand(
            coordinate_reference_change=IfcLocalCoordinateReferenceChange()
        ),
        ConfigureIfcGeometryBasisCommand(
            project_unit_change=IfcProjectUnitChange(length_unit=IfcLengthUnit(prefix="MILLI"))
        ),
        ConfigureIfcGeometryBasisCommand(
            context_operations=(
                UpdateParentContextOperation(context_identifier="Model", precision=1e-5),
            )
        ),
    )
    for command in no_change_commands:
        with pytest.raises(IfcGeometryBasisNoChangeError) as caught:
            compiler.apply(configured, command)
        assert caught.value.code == "IFC_NO_CHANGES"


def test_duplicate_identifier_already_in_exact_ifc_is_integrity_failure() -> None:
    compiler = _compiler()
    configured = compiler.apply(_revision1(), _local_basis_command()).content
    private = _codec().private_copy(configured)
    model = private.model
    parent = model.by_type("IfcGeometricRepresentationContext")[0]
    model.create_entity(
        "IfcGeometricRepresentationSubContext",
        ContextIdentifier="Body",
        ContextType="Model",
        ParentContext=parent,
        TargetView="MODEL_VIEW",
    )
    with pytest.raises(IfcGeometryBasisIntegrityError, match="unique"):
        compiler.inspect_contexts(private.to_bytes())
