from __future__ import annotations

import copy
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    CreateParentContextOperation,
    CreateSubcontextOperation,
    IfcLengthUnit,
    IfcLocalCoordinateReferenceChange,
    IfcPlaneAngleUnit,
    IfcProjectUnitChange,
    IfcProjectedCrsCoordinateReferenceChange,
    IfcWorldCoordinateSystem3D,
)
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.ifc.geometry_basis import (
    IfcOpenShellGeometryBasisCompiler,
)


ROOT = Path(__file__).resolve().parents[2] / "scenarios/ifc_golden_incremental"
ORACLE_PATH = ROOT / "foundation_oracle.py"
PROJECT_GLOBAL_ID = "1xvJu1wbjMywJMjRBpLw$1"
PROJECT_NAME = "Independent Foundation Oracle"


def _oracle_module():
    spec = importlib.util.spec_from_file_location("ifc_g0_foundation_oracle", ORACLE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_revision(*, global_id: str = PROJECT_GLOBAL_ID) -> bytes:
    return (
        IfcOpenShellModelCodec()
        .create_revision1(
            project_global_id=global_id,
            project_name=PROJECT_NAME,
            header_timestamp=datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc),
        )
        .content
    )


def _projected_revision(*, global_id: str = PROJECT_GLOBAL_ID) -> bytes:
    command = ConfigureIfcGeometryBasisCommand(
        project_unit_change=IfcProjectUnitChange(
            length_unit=IfcLengthUnit(prefix="MILLI"),
            plane_angle_unit=IfcPlaneAngleUnit(),
        ),
        coordinate_reference_change=IfcProjectedCrsCoordinateReferenceChange(
            source_context_identifier="Model",
            crs_name="EPSG:4547",
            description="CGCS2000 / 3-degree Gauss-Kruger CM 114E",
            geodetic_datum="China 2000",
            vertical_datum=None,
            map_projection="Gauss-Kruger",
            map_zone="38",
            map_unit=IfcLengthUnit(),
            eastings=500000.0,
            northings=3400000.0,
            orthogonal_height=12.5,
            x_axis_abscissa=0.999847695,
            x_axis_ordinate=0.017452406,
            scale=0.9999,
        ),
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
                world_coordinate_system=IfcWorldCoordinateSystem3D(
                    origin=(125.0, -40.0, 7.5),
                    axis=(0.0, 0.0, 1.0),
                    ref_direction=(1.0, 0.0, 0.0),
                ),
                true_north=(0.0, 1.0),
            ),
        ),
    )
    return (
        IfcOpenShellGeometryBasisCompiler()
        .apply(_base_revision(global_id=global_id), command)
        .content
    )


def _projected_expected(*, include_global_id: bool = True) -> dict[str, object]:
    expected = {
        "schema": "IFC4X3_ADD2",
        "project": {"global_id": PROJECT_GLOBAL_ID, "name": PROJECT_NAME},
        "units": [
            {"unit_type": "LENGTHUNIT", "name": "METRE", "prefix": "MILLI"},
            {"unit_type": "PLANEANGLEUNIT", "name": "RADIAN", "prefix": None},
        ],
        "coordinate_reference": {
            "mode": "projected_crs",
            "source_context_identifier": "Model",
            "crs_name": "EPSG:4547",
            "description": "CGCS2000 / 3-degree Gauss-Kruger CM 114E",
            "geodetic_datum": "China 2000",
            "vertical_datum": None,
            "map_projection": "Gauss-Kruger",
            "map_zone": "38",
            "map_unit": {
                "unit_type": "LENGTHUNIT",
                "name": "METRE",
                "prefix": None,
            },
            "eastings": 500000.0,
            "northings": 3400000.0,
            "orthogonal_height": 12.5,
            "x_axis_abscissa": 0.999847695,
            "x_axis_ordinate": 0.017452406,
            "scale": 0.9999,
        },
        "contexts": [
            {
                "context_identifier": "Body",
                "kind": "subcontext",
                "context_type": "Model",
                "coordinate_space_dimension": 3,
                "parent_context_identifier": "Model",
                "target_view": "MODEL_VIEW",
                "target_scale": None,
                "user_defined_target_view": None,
            },
            {
                "context_identifier": "Model",
                "kind": "parent_context",
                "context_type": "Model",
                "coordinate_space_dimension": 3,
                "precision": 1e-5,
                "world_coordinate_system": {
                    "kind": "3d",
                    "origin": [125.0, -40.0, 7.5],
                    "ref_direction": [1.0, 0.0, 0.0],
                    "axis": [0.0, 0.0, 1.0],
                },
                "true_north": [0.0, 1.0],
            },
        ],
    }
    if not include_global_id:
        expected["project"] = {"name": PROJECT_NAME}
    return expected


def _local_revision_and_expected() -> tuple[bytes, dict[str, object]]:
    command = ConfigureIfcGeometryBasisCommand(
        project_unit_change=IfcProjectUnitChange(
            length_unit=IfcLengthUnit(),
            plane_angle_unit=IfcPlaneAngleUnit(),
        ),
        coordinate_reference_change=IfcLocalCoordinateReferenceChange(),
        context_operations=(
            CreateParentContextOperation(
                context_identifier="Model",
                context_type="Model",
                coordinate_space_dimension=3,
                precision=1e-6,
                world_coordinate_system=IfcWorldCoordinateSystem3D(
                    origin=(0.0, 0.0, 0.0),
                    axis=(0.0, 0.0, 1.0),
                    ref_direction=(1.0, 0.0, 0.0),
                ),
                true_north=None,
            ),
        ),
    )
    content = IfcOpenShellGeometryBasisCompiler().apply(_base_revision(), command).content
    expected = {
        "schema": "IFC4X3_ADD2",
        "project": {"name": PROJECT_NAME},
        "units": [
            {"unit_type": "LENGTHUNIT", "name": "METRE", "prefix": None},
            {"unit_type": "PLANEANGLEUNIT", "name": "RADIAN", "prefix": None},
        ],
        "coordinate_reference": {"mode": "local"},
        "contexts": [
            {
                "context_identifier": "Model",
                "kind": "parent_context",
                "context_type": "Model",
                "coordinate_space_dimension": 3,
                "precision": 1e-6,
                "world_coordinate_system": {
                    "kind": "3d",
                    "origin": [0.0, 0.0, 0.0],
                    "ref_direction": [1.0, 0.0, 0.0],
                    "axis": [0.0, 0.0, 1.0],
                },
                "true_north": None,
            }
        ],
    }
    return content, expected


def test_oracle_independently_accepts_bytes_and_returns_stable_projected_summary() -> None:
    oracle = _oracle_module()
    content = _projected_revision()

    first = oracle.assert_foundation(content, _projected_expected())
    second = oracle.assert_foundation(bytes(content), _projected_expected())

    assert first == second
    assert first["schema"] == "IFC4X3_ADD2"
    assert first["boundary"] == {"site_count": 0, "product_count": 0}
    assert first["server_only"] == {
        "project_global_id": PROJECT_GLOBAL_ID,
        "visibility": "server_only",
    }
    assert [item["context_identifier"] for item in first["contexts"]] == ["Body", "Model"]
    assert re.fullmatch(r"[0-9a-f]{64}", first["semantic_sha256"])


def test_oracle_accepts_an_independently_read_path_and_local_coordinates(tmp_path: Path) -> None:
    oracle = _oracle_module()
    content, expected = _local_revision_and_expected()
    path = tmp_path / "foundation.ifc"
    path.write_bytes(content)

    summary = oracle.assert_foundation(path.read_bytes(), expected)

    assert summary["coordinate_reference"] == {"mode": "local"}
    assert summary["server_only"]["project_global_id"] == PROJECT_GLOBAL_ID


def test_semantic_digest_excludes_server_only_project_global_id() -> None:
    oracle = _oracle_module()
    expected = _projected_expected(include_global_id=False)
    first = oracle.assert_foundation(_projected_revision(), expected)
    second = oracle.assert_foundation(
        _projected_revision(global_id="2xvJu1wbjMywJMjRBpLw$2"),
        expected,
    )

    assert first["server_only"] != second["server_only"]
    assert first["semantic_sha256"] == second["semantic_sha256"]


@pytest.mark.parametrize(
    ("field_path", "wrong_value", "message"),
    (
        (("schema",), "IFC4", r"IFC_SEMANTIC_MISMATCH at schema"),
        (("units", 0, "prefix"), None, r"units\[0\]\.prefix"),
        (("contexts", 1, "precision"), 1e-3, r"contexts\[1\]\.precision"),
        (
            ("contexts", 1, "world_coordinate_system", "origin", 0),
            999.0,
            r"contexts\[1\]\.world_coordinate_system\.origin\[0\]",
        ),
        (("contexts", 1, "true_north", 1), -1.0, r"contexts\[1\]\.true_north\[1\]"),
        (
            ("coordinate_reference", "eastings"),
            0.0,
            r"coordinate_reference\.eastings",
        ),
    ),
)
def test_oracle_reports_precise_semantic_mismatches(
    field_path: tuple[object, ...], wrong_value: object, message: str
) -> None:
    oracle = _oracle_module()
    expected = copy.deepcopy(_projected_expected())
    cursor: object = expected
    for key in field_path[:-1]:
        cursor = cursor[key]  # type: ignore[index]
    cursor[field_path[-1]] = wrong_value  # type: ignore[index]

    with pytest.raises(oracle.FoundationOracleError, match=message):
        oracle.assert_foundation(_projected_revision(), expected)


@pytest.mark.parametrize(
    ("ifc_class", "global_id", "boundary_path"),
    (
        ("IfcSite", "0AAAAAAAAAAAAAAAAAAAAA", "site_count"),
        ("IfcPipeSegment", "0BBBBBBBBBBBBBBBBBBBBB", "product_count"),
    ),
)
def test_oracle_rejects_site_or_product_even_without_configurable_counts(
    ifc_class: str,
    global_id: str,
    boundary_path: str,
) -> None:
    oracle = _oracle_module()
    private = IfcOpenShellModelCodec().private_copy(_projected_revision())
    private.model.create_entity(ifc_class, GlobalId=global_id)

    with pytest.raises(
        oracle.FoundationOracleError,
        match=rf"IFC_G0_BOUNDARY at boundary\.{boundary_path}",
    ):
        oracle.assert_foundation(private.to_bytes(), _projected_expected())


def test_oracle_rejects_projected_map_unit_reusing_the_project_length_unit() -> None:
    oracle = _oracle_module()
    private = IfcOpenShellModelCodec().private_copy(_projected_revision())
    project = private.model.by_type("IfcProject")[0]
    project_length = next(
        unit for unit in project.UnitsInContext.Units if str(unit.UnitType) == "LENGTHUNIT"
    )
    private.model.by_type("IfcProjectedCRS")[0].MapUnit = project_length

    with pytest.raises(
        oracle.FoundationOracleError,
        match=r"IFC_CRS_MAP_UNIT at coordinate_reference\.map_unit",
    ):
        oracle.assert_foundation(private.to_bytes(), _projected_expected())


def test_oracle_rejects_orphan_representation_without_any_product() -> None:
    oracle = _oracle_module()
    private = IfcOpenShellModelCodec().private_copy(_projected_revision())
    body = private.model.by_type("IfcGeometricRepresentationSubContext")[0]
    point = private.model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
    private.model.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body,
        RepresentationIdentifier="Body",
        RepresentationType="GeometricSet",
        Items=(point,),
    )

    assert not private.model.by_type("IfcProduct")
    with pytest.raises(
        oracle.FoundationOracleError,
        match=r"IFC_G0_BOUNDARY at contexts\[\]\.representation_count",
    ):
        oracle.assert_foundation(private.to_bytes(), _projected_expected())


def test_formal_oracle_rejects_host_paths_and_diagnostics_do_not_echo_values(
    tmp_path: Path,
) -> None:
    oracle = _oracle_module()
    path = tmp_path / "not-a-formal-selector.ifc"
    path.write_bytes(_projected_revision())
    with pytest.raises(TypeError, match="exact_artifact_bytes must be bytes"):
        oracle.assert_foundation(path, _projected_expected())
    assert oracle.__all__ == ["FoundationOracleError", "assert_foundation"]

    expected = _projected_expected()
    expected["project"]["name"] = "SENSITIVE-EXPECTED-VALUE"  # type: ignore[index]
    with pytest.raises(oracle.FoundationOracleError) as captured:
        oracle.assert_foundation(_projected_revision(), expected)
    diagnostic = str(captured.value)
    assert captured.value.code == "IFC_SEMANTIC_MISMATCH"
    assert captured.value.path == "project.name"
    assert "SENSITIVE-EXPECTED-VALUE" not in diagnostic
    assert PROJECT_NAME not in diagnostic


def test_oracle_source_has_no_production_codec_writer_database_or_operation_dependency() -> None:
    source = ORACLE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "CAD2BIM_backend",
        "CAD2BIM_mcp",
        "ifc_codec",
        "revision_writer",
        "repository",
        "operation_result",
        "writer_cache",
    ):
        assert forbidden not in source
    assert "ifcopenshell.file.from_string" in source


def test_scenario_descriptors_and_prompts_are_mode_specific_and_do_not_leak_answers() -> None:
    descriptors = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / "descriptors").glob("*.json"))
    }
    assert set(descriptors) == {"diagnostic_guided", "goal_only_semiblind"}
    for name, descriptor in descriptors.items():
        assert descriptor["mode"] == name
        assert descriptor["fresh_session_copy_required"] is True
        assert descriptor["fresh_agent_required"] is True
        prompt_path = (ROOT / "descriptors" / descriptor["prompt_template"]).resolve()
        assert prompt_path.is_relative_to(ROOT.resolve())
        assert prompt_path.is_file()

    guided = (ROOT / "prompts/diagnostic_guided.md").read_text(encoding="utf-8")
    semiblind = (ROOT / "prompts/goal_only_semiblind.md").read_text(encoding="utf-8")
    for method_hint in ("Catalog", "Package descriptions", "Operation history"):
        assert method_hint in guided
        assert method_hint not in semiblind

    injected = "\n".join(
        [guided, semiblind]
        + [json.dumps(value, ensure_ascii=False, sort_keys=True) for value in descriptors.values()]
    )
    for forbidden_pattern in (
        r"\bdirect_[0-9]+\b",
        r"\bdata_[0-9]+\b",
        r"\bop_[0-9]+\b",
        r"EPSG:[0-9]+",
        r"IfcOpenShell",
        r"semantic_sha256",
        r"GlobalId",
        r"eastings",
        r"northings",
    ):
        assert re.search(forbidden_pattern, injected, flags=re.IGNORECASE) is None
    assert PROJECT_NAME not in injected
    assert PROJECT_GLOBAL_ID not in injected
