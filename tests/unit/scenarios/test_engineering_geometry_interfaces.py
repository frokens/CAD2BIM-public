"""Current engineering bundles evaluate interfaces without importing native geometry."""
import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import (
    compile_geometry_interfaces, execute_geometry_interfaces,
)
from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle

ROOT = Path(__file__).resolve().parents[3]
GEOMETRY = ROOT / "tests/scenarios/engineering_product_families/geometry"
FUNCTIONS = tuple(sorted(p.parent.name for p in GEOMETRY.glob("*/interfaces.py")))

def _program(name):
    directory = GEOMETRY / name
    return compile_geometry_interfaces(pack_source_bundle({
        p.name: p.read_bytes() for p in directory.glob("*.py")
    }), "engineering_" + name)

@pytest.mark.parametrize("name", FUNCTIONS)
def test_function_interfaces_keep_current_parameter_samples(name):
    directory = GEOMETRY / name
    sample_file = directory / "validation_samples.json"
    samples = json.loads(sample_file.read_text()) if sample_file.exists() else [{"od_m": .025, "length_m": .1}]
    program = _program(name)
    for parameters in samples:
        interfaces = execute_geometry_interfaces(program, parameters)
        assert interfaces
    tree = ast.parse((directory / "main.py").read_text())
    assert not any(isinstance(node, ast.Attribute) and node.attr == "define_interface" for node in ast.walk(tree))

def test_numeric_interfaces_are_loaded_without_native_modules_in_a_clean_process():
    source = '''
import json, sys
from pathlib import Path
from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import compile_geometry_interfaces, execute_geometry_interfaces
from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle
root=Path("tests/scenarios/engineering_product_families/geometry")
for interface in root.glob("*/interfaces.py"):
    directory=interface.parent
    program=compile_geometry_interfaces(pack_source_bundle({p.name:p.read_bytes() for p in directory.glob("*.py")}),"native_free_"+directory.name)
    sample=directory/"validation_samples.json"
    parameters=json.loads(sample.read_text())[0] if sample.exists() else {"od_m":.025,"length_m":.1}
    execute_geometry_interfaces(program,parameters)
assert not any(name=="cadquery" or name=="OCP" or name.startswith("cadquery.") or name.startswith("OCP.") for name in sys.modules)
'''
    result = subprocess.run([sys.executable, "-c", source], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

def test_riser_preserves_bottom_and_top_outward_frames_and_rejects_wrong_top():
    parameters = json.loads((GEOMETRY / "riser/validation_samples.json").read_text())[0]
    interfaces = {x.interface_key: x for x in execute_geometry_interfaces(_program("riser"), parameters)}
    assert interfaces["bottom"].local_frame.translation_m == tuple(parameters["local_bottom_m"])
    assert interfaces["top"].local_frame.translation_m == tuple(parameters["local_top_m"])
    parameters["local_top_m"][0] += .01
    with pytest.raises(ValueError, match="local top"):
        execute_geometry_interfaces(_program("riser"), parameters)

def test_pipe_preserves_explicit_local_endpoint_references_after_pose_factorization():
    parameters = {"local_points_m": [[0, 0, 0], [2, 0, 0]], "outer_diameter_m": .2,
                  "section": {"known_wall_thickness_m": []},
                  "endpoint_reference_directions": [[0, 1, 0], [0, 1, 0]]}
    program = _program("pipe")
    ports = execute_geometry_interfaces(program, parameters)
    assert len(ports) == 2
    assert all(tuple(row[0] for row in port.local_frame.rotation) == (0, 1, 0) for port in ports)
    parameters["endpoint_reference_directions"][0] = [1, 0, 0]
    with pytest.raises(ValueError, match="perpendicular"):
        execute_geometry_interfaces(program, parameters)

def test_saddle_interface_uses_metres_before_shape_millimetre_conversion():
    parameters = json.loads((GEOMETRY / "service_saddle/validation_samples.json").read_text())[0]
    interfaces = {x.interface_key: x for x in execute_geometry_interfaces(_program("service_saddle"), parameters)}
    expected = tuple(x * parameters["shape"]["branch_port_station_m"] for x in parameters["branch_local_direction"])
    assert interfaces["gas_branch"].local_frame.translation_m == pytest.approx(expected)
    assert interfaces["gas_branch"].profile.diameter_m == parameters["branch_outer_diameter_m"]

def test_tee_near_zero_keeps_logical_source_ports():
    parameters = next(x for x in json.loads((GEOMETRY / "tee_body/validation_samples.json").read_text()) if x["shape"]["mode"] == "near_zero")
    interfaces = {x.interface_key: x for x in execute_geometry_interfaces(_program("tee_body"), parameters)}
    for key, port in parameters["ports"].items():
        assert interfaces[key].local_frame.translation_m == tuple(port["origin_m"])
