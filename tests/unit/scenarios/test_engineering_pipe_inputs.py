"""PIPE projection and construction risks; CAD-derived regressions are explicit."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

from tests.scenarios.engineering_product_families.inputs.pipe import (
    project_pipe_rows,
    provider_request,
    validate_provider_structure,
)


def test_pipe_construction_removes_coordinate_roundoff_but_keeps_real_turns():
    import runpy

    parameters = runpy.run_path(str(Path(__file__).parents[2]
        / "scenarios/engineering_product_families/geometry/pipe/parameters.py"))
    topology = parameters["_sweep_topology"]
    # Frozen CAD-derived local coordinates: the first three intermediate
    # vertices differ from the same straight line only by subtraction noise.
    points = [[0, 0, 0], [-27.67591405343319, -3.933711028143648, 0],
        [-41.09951852852282, -5.841672617385029, 0],
        [-69.53511244043875, -9.88336060453355, 0],
        [-79.74259999766946, -11.334199998527765, 0],
        [-84.43642197643203, -15.179956039802505, 0],
        [-90.36209999769835, -20.035000000149125, 0]]
    original = copy.deepcopy(points)
    assert topology(points) == [0, 4, 6]
    assert points == original
    assert topology([[0., 0., 0.], [1., 1e-8, 0.], [2., 0., 0.]]) == [0, 1, 2]
    assert topology([[0., 0., 0.], [1., 0., 0.], [.5, 0., 0.]]) == [0, 1, 2]
    assert topology([[1e8, 0., 0.], [1e8+1, 1e-7, 0.], [1e8+2, 0., 0.]]) == [0, 1, 2]


def test_pipe_roundoff_bound_does_not_accumulate_along_a_path():
    import runpy

    parameters = runpy.run_path(str(Path(__file__).parents[2]
        / "scenarios/engineering_product_families/geometry/pipe/parameters.py"))
    points = [[float(i), 3e-12*(i/100)**2, 0.] for i in range(101)]
    indices = parameters["_sweep_topology"](points)
    assert 2 < len(indices) < len(points)
    for first, last in zip(indices, indices[1:]):
        chord = parameters["sub"](points[last], points[first])
        for index in range(first+1, last):
            distance = parameters["norm"](parameters["cross"](
                parameters["sub"](points[index], points[first]), chord))/parameters["norm"](chord)
            bound = min(1e-10, 32*max(math.ulp(value)
                for point in (points[first], points[index], points[last]) for value in point))
            assert distance <= bound


@pytest.mark.parametrize("family", ["pipe", "riser", "cross", "gas_meter", "anode"])
def test_single_option_family_scenarios_select_the_declared_binding(family):
    """Single-resource families explicitly select their complete fragment contracts."""
    from CAD2BIM_backend.domain.product_family import ProductFamilyContract
    from CAD2BIM_backend.domain.product_realization import ProductRealizationAuthorFragmentPlacement
    from CAD2BIM_backend.domain.preview_cases import FamilyPreviewCase
    from CAD2BIM_backend.infrastructure.product_realization_runtime.child import _compile_family
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_family_program
    from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle

    root = Path(__file__).parents[2] / "scenarios/engineering_product_families/families" / family
    definition = json.loads((root / "definition.json").read_text())
    ProductFamilyContract.model_validate(definition)
    bindings = {}
    for fragment in definition["geometry_fragments"]:
        assert not {"binding_key", "parameter_sources", "uniform_scale_source"} & fragment.keys()
        option, = fragment["binding_options"]
        bindings[fragment["fragment_key"]] = option["binding_key"]
    program = _compile_family(pack_source_bundle({"main.py": (root / "program/main.py").read_bytes()}),
                              family + "_single_option", "realize",
                              required_entry_names=("realize", "standard_model"))
    for scenario in json.loads((root / "preview_cases.json").read_text())["cases"]["items"]:
        FamilyPreviewCase.model_validate(scenario)
        class Context:
            def param(self, key):
                return scenario["invocation"]["public_parameters"][key]

            def product_origin(self):
                return scenario["invocation"]["product_origin"]

            def contract(self):
                return definition

            def parameters(self):
                return scenario["invocation"]["public_parameters"]

            def external_constraints(self):
                return scenario["invocation"].get("external_constraints", [])

            def policy(self):
                return {}

        entry = "standard_model" if scenario["invocation"]["realization_kind"] == "standard_model" else "realize"
        result = execute_family_program(program, Context(), entry_name=entry)
        assert result["status"] == scenario["expectation"]["expected_status"], scenario["case_key"]
        if result["status"] != "SOLVED":
            reasons = [item.get("violation_key", item.get("ambiguity_key"))
                       for item in result.get("violations", result.get("ambiguities", []))]
            assert sorted(reasons) == sorted(scenario["expectation"]["reason_keys"])
        if result["status"] == "SOLVED":
            placements = [ProductRealizationAuthorFragmentPlacement.model_validate(item)
                          for item in result["fragments"]]
            assert {item.fragment_key: item.binding_key for item in placements} == bindings


def _tables(points: list[list[float]], diameter: float | None = 0.2) -> dict:
    key = "pipe:synthetic:000001"
    return {
        "products": [{
            "product_key": key, "product_kind": "PIPE", "source_entity_key": "synthetic-entity",
            "source_path_key": "synthetic-path", "origin_key": "synthetic-origin",
            "source_disposition_key": "synthetic-disposition", "planned_pipe_sections": [],
        }],
        "axis_vertices": [{
            "product_key": key, "source_path_key": "synthetic-path",
            "source_entity_key": "synthetic-entity", "axis_vertex_key": f"vertex-{i}",
            "sequence_ordinal": i, "source_segment_ordinal": max(1, i - 1),
            "source_parameter": None, "geometry_element_key": None, "authority_key": "synthetic",
            "x_m": point[0], "y_m": point[1], "z_m": point[2],
        } for i, point in enumerate(points, 1)],
        "geometry_inputs": [{
            "product_key": key, "geometry_input_key": "synthetic-geometry",
            "source_path_key": "synthetic-path", "pipe_piece_key": key,
            "origin_key": "synthetic-origin", "input_kind": "PIPE_SOURCE_PATH_FACTS",
            "status": "known" if diameter is not None else "partial",
            "outer_diameter_m": diameter,
            "diameter_status": "known" if diameter is not None else "missing",
            "diameter_authority": "synthetic" if diameter is not None else None,
        }],
        "product_origins": [{
            "product_key": key, "origin_key": "synthetic-origin", "source_path_key": "synthetic-path",
            "origin_status": "known", "origin_x_m": 5.0, "origin_y_m": 7.0,
            "origin_z_m": 1.0, "authority_key": "synthetic",
        }],
        "source_dispositions": [{
            "source_disposition_key": "synthetic-disposition",
            "source_entity_key": "synthetic-entity", "ingestion_disposition": "included",
            "modeling_disposition": "modeled", "reason_code": None,
        }],
        "unresolved_items": [], "connection_segments": [], "ports": [],
    }


def test_world_path_and_nonzero_origin_are_passed_separately() -> None:
    points = [[0.0, 0.0, 0.0], [2.0, 0.5, 0.0], [4.0, 0.0, 0.0]]
    record = project_pipe_rows(_tables(points))[0]
    request = provider_request(record, "cur.synthetic-test", "direct_001")
    invocation = json.loads(request["invocation_json"])
    public = invocation["public_parameters"]

    assert record["world_points_m"] == points
    assert public["middle_points_m"] == points[1:-1]
    assert "world_points_m" not in public and "outer_diameter_m" not in public
    inlet, outlet = invocation["external_constraints"]
    assert inlet["condition"]["point_m"] == points[0]
    assert outlet["condition"]["point_m"] == points[-1]
    assert inlet["condition"]["outer_diameter_m"] == outlet["condition"]["outer_diameter_m"] == 0.2
    assert invocation["product_origin"] == [5.0, 7.0, 1.0]
    assert public["section"] == {"known_wall_thickness_m": []}
    assert record["input_ready"] is True
    assert record["family_geometry_allowed"] is True


@pytest.mark.parametrize("family,expected", [
    ("pipe", {"local_points_m": [[0, 0, 0], [2, 0, 0], [2, 3, 0]], "outer_diameter_m": 0.2}),
    ("riser", {"local_bottom_m": [0, 0, 0], "local_top_m": [0, 0, 3],
               "height_m": 3.0, "outer_diameter_m": 0.25}),
])
def test_nominal_models_need_only_product_parameters(family, expected):
    import runpy
    from CAD2BIM_backend.application.product_realization_input_service import ProductRealizationInputService
    from CAD2BIM_backend.domain.product_family import ProductFamilyContract

    root = Path(__file__).parents[2] / "scenarios/engineering_product_families/families" / family
    contract = ProductFamilyContract.model_validate(json.loads((root / "definition.json").read_text()))
    normalized = ProductRealizationInputService.normalize_standard_definition(contract)
    public = normalized.standard_model.public_parameters
    assert public == ({"middle_points_m": [], "section": {"known_wall_thickness_m": []}}
                      if family == "pipe" else {"section": {"known_wall_thickness_m": []}})

    class Context:
        def param(self, key):
            assert key == "section"
            return public[key]

        def product_origin(self):
            return [7, 8, 9]

        def external_constraints(self):
            raise AssertionError("standard_model must not read caller installation targets")

    result = runpy.run_path(str(root / "program/main.py"))["standard_model"](Context())
    assert result["status"] == "SOLVED"
    for key, value in expected.items():
        assert result["internal_parameters"][key] == value


@pytest.mark.parametrize("family,axis_key", [("pipe", "inlet"), ("riser", "bottom")])
def test_fixed_endpoint_solver_rejects_direction_and_diameter_conflicts(family, axis_key):
    import runpy

    root = Path(__file__).parents[2] / "scenarios/engineering_product_families/families" / family
    scenarios = json.loads((root / "preview_cases.json").read_text())["cases"]["items"]
    invocation = copy.deepcopy(next(case["invocation"] for case in scenarios
                                    if case["case_key"] == ("p1_straight" if family == "pipe" else "r1_short")))

    class Context:
        def param(self, key):
            return invocation["public_parameters"][key]

        def product_origin(self):
            return invocation["product_origin"]

        def external_constraints(self):
            return invocation["external_constraints"]

    realize = runpy.run_path(str(root / "program/main.py"))["realize"]
    assert realize(Context())["status"] == "SOLVED"
    target = next(item["condition"] for item in invocation["external_constraints"] if item["interface_key"] == axis_key)
    original = list(target["axis"])
    target["axis"] = [-value for value in original]
    assert realize(Context())["violations"][0]["violation_key"] == "endpoint_direction"
    target["direction_relation"] = "same"
    assert realize(Context())["status"] == "SOLVED"
    target["outer_diameter_m"] *= 2
    assert realize(Context())["violations"][0]["violation_key"] == "endpoint_diameter"


def test_missing_diameter_is_preserved_and_blocks_request() -> None:
    tables = _tables([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], diameter=None)
    tables["unresolved_items"] = [{
        "product_key": tables["products"][0]["product_key"],
        "unresolved_item_key": "synthetic-issue", "issue_code": "diameter_missing",
        "subject_key": "synthetic-segment", "affected_field": "outer_diameter_m",
        "resolution_status": "open", "blocked_actions": ["family_geometry"], "authority_key": None,
    }]
    record = project_pipe_rows(tables)[0]

    assert record["outer_diameter_m"] is None
    assert record["diameter_status"] == "missing"
    assert record["input_readiness_reasons"] == ["outer_diameter_missing_or_invalid"]
    assert record["family_geometry_allowed"] is False
    assert [item["kind"] for item in record["family_geometry_blockers"]] == [
        "input_not_ready", "source_action_blocked",
    ]
    with pytest.raises(ValueError, match="not ready"):
        provider_request(record, "family-ref", "direct_001")


def test_capacity_blocks_without_truncating_and_pending_window_stays_pending() -> None:
    points = [[float(i), 0.0, 0.0] for i in range(129)]
    tables = _tables(points)
    section = {
        "section_key": "synthetic-section", "source_segment_key": "synthetic-segment",
        "window_status": "pending_geometry", "window_start": None, "window_end": None,
    }
    tables["products"][0]["planned_pipe_sections"] = [copy.deepcopy(section)]
    tables["connection_segments"] = [{
        "pipe_piece_key": tables["products"][0]["product_key"],
        "segment_key": "synthetic-segment", "source_path_key": "synthetic-path",
        "diameter_status": "known", "outer_diameter_m": 0.2,
    }]
    record = project_pipe_rows(tables)[0]

    assert record["world_points_m"] == points
    assert record["input_ready"] is False
    assert record["family_geometry_allowed"] is False
    assert record["input_readiness_reasons"] == ["parameter_capacity_over_128_points"]
    assert record["planned_pipe_sections"] == [section]
    assert record["final_retained_sections_ready"] is False
    assert record["final_retained_sections_blockers"] == ["pending_geometry_window"]


def test_open_family_geometry_issue_blocks_structurally_ready_planned_parent() -> None:
    tables = _tables([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    key = tables["products"][0]["product_key"]
    tables["products"][0]["planned_pipe_sections"] = [{
        "section_key": "synthetic-section", "source_segment_key": "synthetic-segment",
        "window_status": "pending_geometry", "window_start": None, "window_end": None,
    }]
    tables["connection_segments"] = [{
        "pipe_piece_key": key, "segment_key": "synthetic-segment",
        "source_path_key": "synthetic-path", "diameter_status": "known",
        "outer_diameter_m": 0.2,
    }]
    tables["unresolved_items"] = [{
        "product_key": key, "unresolved_item_key": "synthetic-planned-issue",
        "issue_code": "synthetic_pending", "subject_key": key,
        "affected_field": "planned_pipe_sections", "resolution_status": "open",
        "blocked_actions": ["family_geometry", "ifc_network_binding"],
        "authority_key": "synthetic-authority",
    }]
    record = project_pipe_rows(tables)[0]

    assert record["input_ready"] is True
    validate_provider_structure(record, "cur.synthetic-test", "direct_001")
    assert record["family_geometry_allowed"] is False
    assert record["family_geometry_blockers"] == [{
        "kind": "source_action_blocked", "source_member": "unresolved_items",
        "unresolved_item_key": "synthetic-planned-issue",
        "issue_code": "synthetic_pending", "affected_field": "planned_pipe_sections",
        "blocked_actions": ["family_geometry", "ifc_network_binding"],
        "authority_key": "synthetic-authority",
    }]
    with pytest.raises(ValueError, match="blocks family_geometry"):
        provider_request(record, "cur.synthetic-test", "direct_001")


def test_network_only_issue_does_not_block_own_geometry() -> None:
    tables = _tables([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    tables["unresolved_items"] = [{
        "product_key": tables["products"][0]["product_key"],
        "unresolved_item_key": "synthetic-host-issue", "issue_code": "synthetic_host",
        "subject_key": "external-host", "affected_field": "host_port",
        "resolution_status": "open", "blocked_actions": ["ifc_network_binding"],
        "authority_key": "synthetic-authority",
    }]
    record = project_pipe_rows(tables)[0]

    assert record["family_geometry_allowed"] is True
    assert record["family_geometry_blockers"] == []
    assert provider_request(record, "cur.synthetic-test", "direct_001")["mode"] == "single"
