from __future__ import annotations

from datetime import datetime, timezone

import pytest

from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    CreateParentContextOperation,
    IfcLengthUnit,
    IfcLocalCoordinateReferenceChange,
    IfcPlaneAngleUnit,
    IfcProjectUnitChange,
    IfcWorldCoordinateSystem3D,
)
from CAD2BIM_backend.domain.ifc_project import (
    IfcProjectIntegrityError,
    IfcProjectNoChangeError,
    IfcProjectPatch,
    UpdateIfcProjectCommand,
)
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc.geometry_basis import (
    IfcOpenShellGeometryBasisCompiler,
)
from CAD2BIM_backend.infrastructure.ifc.project import IfcOpenShellProjectCompiler


PROJECT_GLOBAL_ID = "1xvJu1wbjMywJMjRBpLw$1"
PROJECT_NAME = "Project Test"
HEADER_TIMESTAMP = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


def _codec() -> IfcOpenShellModelCodec:
    return IfcOpenShellModelCodec()


def _compiler() -> IfcOpenShellProjectCompiler:
    return IfcOpenShellProjectCompiler(_codec())


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


def _configured_revision() -> bytes:
    return IfcOpenShellGeometryBasisCompiler(_codec()).apply(
        _revision1(),
        ConfigureIfcGeometryBasisCommand(
            project_unit_change=IfcProjectUnitChange(
                length_unit=IfcLengthUnit(prefix="MILLI"),
                plane_angle_unit=IfcPlaneAngleUnit(),
            ),
            coordinate_reference_change=IfcLocalCoordinateReferenceChange(),
            context_operations=(
                CreateParentContextOperation(
                    context_identifier="Model",
                    context_type="Model",
                    coordinate_space_dimension=3,
                    precision=1e-5,
                    world_coordinate_system=IfcWorldCoordinateSystem3D(
                        origin=(0.0, 0.0, 0.0),
                        axis=(0.0, 0.0, 1.0),
                        ref_direction=(1.0, 0.0, 0.0),
                    ),
                ),
            ),
        ),
    ).content


def test_compiler_updates_all_five_business_fields_and_reads_saved_candidate() -> None:
    compiler = _compiler()
    base = _revision1()
    encoded = compiler.apply(
        base,
        UpdateIfcProjectCommand(
            patch=IfcProjectPatch(
                name="Gas Network",
                description="Authoritative network model",
                object_type="Distribution Project",
                long_name="City Gas Distribution Network",
                phase="As Built",
            )
        ),
    )

    assert base == _revision1()
    assert encoded.content != base
    assert compiler.inspect_project(encoded.content).model_dump() == {
        "global_id": PROJECT_GLOBAL_ID,
        "name": "Gas Network",
        "description": "Authoritative network model",
        "object_type": "Distribution Project",
        "long_name": "City Gas Distribution Network",
        "phase": "As Built",
    }
    assert encoded.inspection.project_global_id == PROJECT_GLOBAL_ID
    assert encoded.inspection.project_name == "Gas Network"


def test_clear_fields_only_clear_nullable_business_attributes() -> None:
    compiler = _compiler()
    populated = compiler.apply(
        _revision1(),
        UpdateIfcProjectCommand(
            patch=IfcProjectPatch(
                description="description",
                object_type="object type",
                long_name="long name",
                phase="phase",
            )
        ),
    ).content
    cleared = compiler.apply(
        populated,
        UpdateIfcProjectCommand(
            patch=IfcProjectPatch(description="kept"),
            clear_fields=("object_type", "long_name", "phase"),
        ),
    )

    summary = compiler.inspect_project(cleared.content)
    assert summary.name == PROJECT_NAME
    assert summary.description == "kept"
    assert summary.object_type is None
    assert summary.long_name is None
    assert summary.phase is None


def test_semantic_no_change_does_not_emit_candidate_revision() -> None:
    compiler = _compiler()
    base = _revision1()
    with pytest.raises(IfcProjectNoChangeError) as caught:
        compiler.apply(
            base,
            UpdateIfcProjectCommand(patch=IfcProjectPatch(name=PROJECT_NAME)),
        )
    assert caught.value.code == "IFC_NO_CHANGES"


def test_project_update_preserves_global_id_geometry_basis_and_other_entities() -> None:
    compiler = _compiler()
    base = _configured_revision()
    before_model = _codec().private_copy(base).model
    before_project = before_model.by_type("IfcProject")[0]
    before_other_entities = tuple(
        (entity.id(), str(entity)) for entity in before_model if entity != before_project
    )
    before_units_id = before_project.UnitsInContext.id()
    before_context_ids = tuple(
        context.id() for context in before_project.RepresentationContexts
    )

    updated = compiler.apply(
        base,
        UpdateIfcProjectCommand(
            patch=IfcProjectPatch(description="Metadata only", phase="Construction")
        ),
    ).content
    after_model = _codec().private_copy(updated).model
    after_project = after_model.by_type("IfcProject")[0]

    assert after_project.GlobalId == PROJECT_GLOBAL_ID
    assert after_project.OwnerHistory is None
    assert after_project.UnitsInContext.id() == before_units_id
    assert tuple(context.id() for context in after_project.RepresentationContexts) == (
        before_context_ids
    )
    assert tuple(
        (entity.id(), str(entity)) for entity in after_model if entity != after_project
    ) == before_other_entities
    assert IfcOpenShellGeometryBasisCompiler(_codec()).inspect_geometry_basis(updated) == (
        IfcOpenShellGeometryBasisCompiler(_codec()).inspect_geometry_basis(base)
    )


def test_inspector_rejects_invalid_project_cardinality() -> None:
    private = _codec().private_copy(_revision1())
    private.model.create_entity(
        "IfcProject",
        GlobalId="3H5lXW9mf8FQDsA1cTbK2_",
        Name="Second Project",
    )
    with pytest.raises(IfcProjectIntegrityError, match="exactly one"):
        _compiler().inspect_project(private.to_bytes())
