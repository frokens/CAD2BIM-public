"""Independent nominal sphere contract and existing interface-owner checks."""

import copy
import json
import math
from pathlib import Path

import pytest

from CAD2BIM_backend.domain.geometry_runtime import normalize_geometry_function_parameters
from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import (
    execute_geometry_build, compile_geometry_interfaces, execute_geometry_interfaces,
)
from CAD2BIM_backend.infrastructure.product_realization_runtime.child import _compile_geometry
from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle


ROOT = Path(__file__).resolve().parents[3]
GEOMETRY = ROOT / "tests/scenarios/engineering_product_families/geometry/connection_sphere"


def _parameters():
    return json.loads((GEOMETRY / "validation_samples.json").read_text())[0]


def _build(parameters):
    bundle = pack_source_bundle({p.name: p.read_bytes() for p in GEOMETRY.glob("*.py")})
    interfaces = execute_geometry_interfaces(compile_geometry_interfaces(bundle, "sphere_interfaces"), parameters)
    program = _compile_geometry(
        bundle,
        "connection_sphere_owner", "build",
    )
    shape, _ = execute_geometry_build(program, parameters, allow_interfaces=False)
    return shape, interfaces


def test_connection_sphere_preserves_independent_circular_and_datum_interfaces():
    schema = json.loads((GEOMETRY / "parameter_schema.json").read_text())
    for parameters in json.loads((GEOMETRY / "validation_samples.json").read_text()):
        normalized = normalize_geometry_function_parameters(schema, parameters)
        shape, interfaces = _build(normalized)
        assert shape.isValid() and len(shape.Solids()) == 1
        assert shape.Volume() == pytest.approx(math.pi * parameters["diameter_m"] ** 3 / 6)
        assert all(face.geomType() == "SPHERE" for face in shape.Faces())
        actual = {item.interface_key: item for item in interfaces}
        for expected in parameters["interfaces"]:
            receipt = actual[expected["key"]]
            assert list(receipt.local_frame.translation_m) == expected["origin_m"]
            rotation = receipt.local_frame.rotation
            assert [rotation[i][2] for i in range(3)] == expected["axis"]
            assert [rotation[i][0] for i in range(3)] == expected["reference_direction"]
            if expected["kind"] == "datum":
                assert receipt.profile is None
            else:
                assert receipt.profile.diameter_m == expected["diameter_m"]


@pytest.mark.parametrize("fault", ["duplicate", "parallel_frame"])
def test_connection_sphere_keeps_existing_interface_validation(fault):
    parameters = _parameters()
    if fault == "duplicate":
        parameters["interfaces"][1]["key"] = parameters["interfaces"][0]["key"]
    else:
        parameters["interfaces"][1]["reference_direction"] = [0, 0, 1]
    with pytest.raises(ValueError):
        _build(parameters)


@pytest.mark.parametrize("fault", ["datum_profile", "unknown_kind", "too_many"])
def test_connection_sphere_schema_closes_branches_and_bounds_interfaces(fault):
    parameters = _parameters()
    if fault == "datum_profile":
        parameters["interfaces"][0]["diameter_m"] = 0.2
    elif fault == "unknown_kind":
        parameters["interfaces"][0]["kind"] = "sphere"
    else:
        parameters["interfaces"] = [copy.deepcopy(parameters["interfaces"][0])] * 65
    schema = json.loads((GEOMETRY / "parameter_schema.json").read_text())
    with pytest.raises(ValueError):
        normalize_geometry_function_parameters(schema, parameters)


@pytest.mark.parametrize("resource", ["curved_two_port", "connector_body", "tee_body", "service_saddle"])
def test_detailed_resources_reject_product_specific_node_dispatch(resource):
    directory = GEOMETRY.parent / resource
    schema = json.loads((directory / "parameter_schema.json").read_text())
    samples = json.loads((directory / "validation_samples.json").read_text())
    for parameters in samples:
        assert normalize_geometry_function_parameters(schema, parameters) == parameters
    invalid = copy.deepcopy(samples[0])
    invalid["shape"] = {"mode": "node" if resource == "service_saddle" else "sphere",
                        "diameter_m": .2}
    with pytest.raises(ValueError):
        normalize_geometry_function_parameters(schema, invalid)
