"""Focused numeric-to-shape checks for the current TEE branch recipe."""

import json
import math
import runpy
import importlib
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[3]
FAMILY = ROOT / "tests/scenarios/engineering_product_families/families/tee/program/main.py"
GEOMETRY = ROOT / "tests/scenarios/engineering_product_families/geometry/tee_body/main.py"
SCENARIOS = ROOT / "tests/scenarios/engineering_product_families/families/tee/preview_cases.json"

def _geometry_bundle():
    from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle
    return pack_source_bundle({p.name: p.read_bytes() for p in GEOMETRY.parent.glob("*.py")})

def _geometry_interfaces(parameters):
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import compile_geometry_interfaces, execute_geometry_interfaces
    return execute_geometry_interfaces(compile_geometry_interfaces(_geometry_bundle(), "tee_interfaces_owner"), parameters)


def _family():
    from CAD2BIM_backend.domain.product_realization import (
        MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS, MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES,
        MAX_PRODUCT_REALIZATION_SOURCE_TOTAL_BYTES)
    from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle
    from CAD2BIM_backend.infrastructure.worker_runtime.python_bundle import compile_python_bundle
    from CAD2BIM_backend.infrastructure.worker_runtime.python_user_code import RestrictedImportGuard
    manifest = json.loads((FAMILY.parents[1] / "manifest.json").read_text())
    base = FAMILY.parents[3]
    bundle = pack_source_bundle({item["bundle_path"]: (base/item["repo_path"]).read_bytes()
                                 for item in manifest["family"]["source_files"]})
    program = compile_python_bundle(bundle, package_name="tee_owner", entry_name="realize",
        required_entry_names=("realize", "standard_model"), allowed_import_roots={"math"},
        profile_label="TEE owner numeric", maximum_members=MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS,
        maximum_member_bytes=MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES,
        maximum_total_bytes=MAX_PRODUCT_REALIZATION_SOURCE_TOTAL_BYTES)
    namespace = program.load_entry(RestrictedImportGuard(allowed_import_roots={"math"})).__globals__
    for entry in ("realize", "standard_model"):
        author_entry = namespace[entry]
        def canonical_entry(context, author_entry=author_entry, entry=entry):
            proposal = author_entry(context)
            return _canonical_tee_proposal(context, proposal, entry=entry) if proposal["status"] == "SOLVED" else proposal
        namespace[entry] = canonical_entry
    return namespace


def _canonical_tee_proposal(context, proposal, *, entry="realize"):
    from CAD2BIM_backend.domain.product_family import ProductFamilyContract
    from tests.unit.scenarios.family_numeric_fixtures import canonical_numeric_proposal
    contract = ProductFamilyContract.model_validate_json((FAMILY.parents[1] / "definition.json").read_text())
    return canonical_numeric_proposal(FAMILY.parents[3], "tee", contract, context, proposal,
        realization_kind="standard_model" if entry == "standard_model" else "self_geometry")


class FamilyContext:
    def __init__(self, source_facts, origin, external_constraints=None):
        self._parameters = {key: value for key, value in source_facts.items() if key.endswith("_role")}
        self.origin = origin
        self.targets = external_constraints
        if self.targets is None:
            self.targets = [{"interface_key": "side_" + key, "condition": {
                "kind": "finite_segment", "start_m": source_facts["side_" + key + "_a_m"],
                "end_m": source_facts["side_" + key + "_b_m"],
                "outer_diameter_m": source_facts["side_" + key + "_outer_diameter_m"],
                "direction_relation": "same"}} for key in "abc"]

    def param(self, key):
        return self._parameters[key]

    def parameters(self):
        return self._parameters

    def external_constraints(self):
        return self.targets

    def contract(self):
        return {}

    def policy(self):
        return {}

    def product_origin(self):
        return self.origin


class GeometryContext:
    def __init__(self, parameters):
        self.parameters = parameters
        self.interfaces = []

    def param(self, key):
        return self.parameters[key]

    def define_interface(self, **interface):
        self.interfaces.append(interface)


def _realize(parameters, origin, external_constraints=None):
    prepared = time.perf_counter()
    family = _family()
    preparation_seconds = time.perf_counter()-prepared
    started = time.perf_counter()
    proposal = family["realize"](FamilyContext(parameters, origin, external_constraints))
    execution_seconds = time.perf_counter()-started
    assert proposal["status"] == "SOLVED", proposal
    print("TEE_FAMILY_PREPARATION_SECONDS", preparation_seconds)
    print("TEE_FAMILY_SECONDS", execution_seconds)
    return proposal["fragments"][0]["geometry_parameters"]


def _check_geometry(parameters, *, controlled=False):
    prepared = time.perf_counter()
    cq = pytest.importorskip("cadquery")
    preparation_seconds = time.perf_counter()-prepared
    prepared = time.perf_counter()
    context = GeometryContext(parameters)
    operations = []
    if controlled:
        from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
        from CAD2BIM_backend.infrastructure.product_realization_runtime.child import _compile_geometry
        from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle
        program = _compile_geometry(_geometry_bundle(),
                                    "tee_controlled_owner", "build")
        source_preparation_seconds = time.perf_counter()-prepared
        started = time.perf_counter()
        interfaces = _geometry_interfaces(parameters)
        shape, _ = execute_geometry_build(program, parameters, allow_interfaces=False)
        geometry_seconds = time.perf_counter()-started
        context.interfaces = [{"key": item.interface_key,
                               "origin_m": list(item.local_frame.translation_m),
                               "axis": [row[2] for row in item.local_frame.rotation],
                               "profile": item.profile.model_dump(mode="json")}
                              for item in interfaces]
    else:
        importlib.import_module("tests.scenarios.engineering_product_families.geometry.tee_body.interfaces").evaluate(context)
        build = importlib.import_module("tests.scenarios.engineering_product_families.geometry.tee_body.main").build
        source_preparation_seconds = time.perf_counter()-prepared
        fuse, cut = cq.Shape.fuse, cq.Shape.cut
        compound_fuse, compound_cut = cq.Compound.fuse, cq.Compound.cut
        def operation(name, method, shape, *args, **kwargs):
            started = time.perf_counter()
            result = method(shape, *args, **kwargs)
            operations.append({"operation": name, "seconds": time.perf_counter()-started,
                               "operand_count": len(args)})
            return result
        with patch.object(cq.Shape, "fuse", lambda self, *a, **kw: operation("fuse", fuse, self, *a, **kw)), \
             patch.object(cq.Shape, "cut", lambda self, *a, **kw: operation("cut", cut, self, *a, **kw)), \
             patch.object(cq.Compound, "fuse", lambda self, *a, **kw: operation("fuse", compound_fuse, self, *a, **kw)), \
             patch.object(cq.Compound, "cut", lambda self, *a, **kw: operation("cut", compound_cut, self, *a, **kw)):
            started = time.perf_counter()
            shape = build(context)
            geometry_seconds = time.perf_counter()-started
    print("TEE_GEOMETRY_EVIDENCE", json.dumps({"parameters": parameters,
        "cadquery_import_seconds": preparation_seconds,
        "source_preparation_seconds": source_preparation_seconds, "geometry_seconds": geometry_seconds,
        "operations": operations}))
    # build already checks the final validity/solid/shell once each. Reuse this
    # body for independent terminal material and communicating-bore probes.
    assert {interface["key"] for interface in context.interfaces} == {
        "side_a", "side_b", "side_c"
    }
    main_keys = (parameters["shape"]["main_a_key"], parameters["shape"]["main_b_key"])
    fmain = 1.1*max(parameters["ports"][f"side_{key}"]["outer_diameter_m"] for key in main_keys)
    fmax = 1.1*max(parameters["ports"][f"side_{key}"]["outer_diameter_m"] for key in "abc")
    faces = shape.Faces()
    for key in "abc":
        diameter = parameters["ports"][f"side_{key}"]["outer_diameter_m"]
        port = cq.Vector(*parameters["ports"][f"side_{key}"]["origin_m"])
        axis = cq.Vector(*parameters["ports"][f"side_{key}"]["axis"])
        radial = cq.Vector(*parameters["ports"][f"side_{key}"]["reference_direction"])
        thickness = (1.1*diameter+(fmain if key in main_keys else fmax))*.05
        inside = port-axis*(.05*thickness)
        assert not shape.isInside(inside)
        assert shape.isInside(inside+radial*(.49*diameter))
        face = next(face for face in faces if face.geomType() == "PLANE" and (face.Center()-port).Length < 1e-8)
        assert face.normalAt().dot(axis) > 1-1e-7
        interface = next(item for item in context.interfaces if item["key"] == f"side_{key}")
        assert interface["origin_m"] == pytest.approx(port.toTuple(), abs=1e-8)
        assert interface["axis"] == pytest.approx(axis.toTuple(), abs=1e-8)
        assert interface["profile"] == {"kind": "circular", "diameter_m": diameter}
    family = _family()
    a, b, branch = (parameters["shape"][key] for key in ("main_a_key", "main_b_key", "branch_key"))
    ua, ub, uv = (parameters["ports"][f"side_{key}"]["axis"] for key in (a, b, branch))
    center = cq.Vector(*parameters["shape"]["local_center_m"])
    bend, neck, turn = (parameters["shape"].get(key, 0.0) for key in ("bend_scale_m", "branch_neck_scale_m", "turn_radian"))
    middle = center+cq.Vector(*family["main_middle"]({"u": ua}, {"u": ub}, bend, turn))
    controls = [middle]+[center+cq.Vector(*uv)*(factor*neck) for factor in (.7, .9, 1.0)]
    for t in (.2, .5, .8):
        branch_point = sum((point*weight for point, weight in zip(controls,
            ((1-t)**3, 3*(1-t)**2*t, 3*(1-t)*t*t, t**3))), cq.Vector())
        assert not shape.isInside(branch_point)
        if turn:
            flow = cq.Vector(*ua)*-1
            normal = flow.cross(cq.Vector(*ub)).normalized()
            radial = normal.cross(flow).normalized()
            radius = parameters["shape"]["arc_radius_m"]
            point = center+cq.Vector(*ua)*(.9*bend)+flow*(radius*math.sin(turn*t))+radial*(radius*(1-math.cos(turn*t)))
        else:
            point = center+cq.Vector(*ub)*(bend*(2*t-1))
        assert not shape.isInside(point)
    return shape


@pytest.mark.parametrize("scenario_key,expected_mode", [
    ("t1_known_straight", "straight"),
    ("t2_known_curved", "curved"),
    ("a_curvature_boundary", "curved"),
])
def test_existing_tee_representative_shapes(scenario_key, expected_mode):
    scenarios = json.loads(SCENARIOS.read_text())["cases"]["items"]
    case = next(item for item in scenarios if item["case_key"] == scenario_key)
    parameters = _realize(case["invocation"]["public_parameters"], case["invocation"]["product_origin"], case["invocation"].get("external_constraints", ()))
    assert parameters["shape"]["mode"] == expected_mode
    _check_geometry(parameters, controlled=scenario_key == "t2_known_curved")


# Private drawing-specific regression omitted from public distribution.


def _source_with_main_turn(source, center, degrees):
    changed = deepcopy(source)
    direction = [center[i]-source["side_a_b_m"][i] for i in range(2)]
    length = math.hypot(*direction)
    direction = [item/length for item in direction]
    original_length = math.dist(center, source["side_b_b_m"])
    theta = math.radians(degrees)
    changed["side_b_b_m"] = [
        center[0]+original_length*(direction[0]*math.cos(theta)-direction[1]*math.sin(theta)),
        center[1]+original_length*(direction[0]*math.sin(theta)+direction[1]*math.cos(theta)), 0.0]
    return changed


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


def test_near_zero_tee_registration_sample_runs_in_controlled_modeling_arena():
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    from CAD2BIM_backend.infrastructure.product_realization_runtime.child import _compile_geometry
    from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle

    sample = json.loads((GEOMETRY.parent / "validation_samples.json").read_text())[0]
    assert sample["shape"]["mode"] == "near_zero"
    program = _compile_geometry(_geometry_bundle(),
                                "tee_controlled_registration_sample", "build")
    interfaces = _geometry_interfaces(sample)
    shape, _ = execute_geometry_build(program, sample, allow_interfaces=False)
    assert shape.isValid() and len(shape.Solids()) == len(shape.Shells()) == 1
    assert {item.interface_key for item in interfaces} == {"side_a", "side_b", "side_c"}


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


def test_tee_source_continuity_keeps_valid_known_curved_pair():
    case = next(row for row in json.loads(SCENARIOS.read_text())["cases"]["items"]
                if row["case_key"] == "t2_known_curved")
    proposal = _family()["realize"](FamilyContext(case["invocation"]["public_parameters"], case["invocation"]["product_origin"], case["invocation"].get("external_constraints", ())))
    shape = proposal["fragments"][0]["geometry_parameters"]["shape"]
    assert shape["mode"] == "curved"
    assert (shape["main_a_key"], shape["main_b_key"], shape["branch_key"]) == ("a", "b", "c")


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


@pytest.mark.parametrize("scenario_key", [
    "standard_nominal", "standard_placed", "t1_known_straight", "t2_known_curved",
    "t3_independent_curved", "joint_bk_clearance", "a_curvature_boundary", "t4_tied_main_pair",
    "no_common_intersection", "strict_short_windows", "outside_main_bend_domain",
])
def test_tee_numeric_representatives(scenario_key):
    case = next(item for item in json.loads(SCENARIOS.read_text())["cases"]["items"]
                if item["case_key"] == scenario_key)
    family = _family()
    entry = "standard_model" if case["invocation"]["realization_kind"] == "standard_model" else "realize"
    proposal = family[entry](FamilyContext(
        case["invocation"]["public_parameters"], case["invocation"]["product_origin"], case["invocation"].get("external_constraints", ())))
    assert proposal["status"] == case["expectation"]["expected_status"], proposal
    if scenario_key == "joint_bk_clearance":
        parameters = proposal["fragments"][0]["geometry_parameters"]
        assert (parameters["shape"]["bend_scale_m"], parameters["shape"]["branch_neck_scale_m"]) != pytest.approx((0.11, 0.308))


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


def test_larger_branch_profile_keeps_main_root_step():
    case = next(item for item in json.loads(SCENARIOS.read_text())["cases"]["items"] if item["case_key"] == "t1_known_straight")
    public = case["invocation"]["public_parameters"]
    targets = case["invocation"]["external_constraints"]
    targets[0]["condition"]["start_m"] = [-1.0, 0.0, 0.0]
    targets[1]["condition"]["start_m"] = [1.0, 0.0, 0.0]
    targets[2]["condition"]["outer_diameter_m"] = .4
    parameters = _realize(public, case["invocation"]["product_origin"], targets)
    shape = _check_geometry(parameters)
    cq = pytest.importorskip("cadquery")
    bend = parameters["shape"]["bend_scale_m"]
    for sign in (-1, 1):
        assert not shape.isInside(cq.Vector(sign*(bend-1e-4), 0, .165))
        assert shape.isInside(cq.Vector(sign*(bend+1e-4), 0, .165))


def test_radial_profile_retains_steps_and_overlap_crossing():
    pytest.importorskip("cadquery")
    profile = runpy.run_path(str(GEOMETRY))["radial_profile"]([
        (0, 1, .1, .1), (1, 3, .4, .2), (1.5, 2.5, .3, .3)])
    for expected in ((1, .1), (1, .4), (2, .3), (2.5, .3), (2.5, .25)):
        assert any(point == pytest.approx(expected) for point in profile)


def test_collar_capsule_support_preserves_sphere_certificate_and_coincident_boundary():
    family = _family()
    gap = family["collar_capsule_gap"]
    center, axis = [0.0, 2.0, 0.0], [0.0, 1.0, 0.0]
    start, end = [-1.0, 0.0, 0.0], [1.0, 0.0, 0.0]
    old_sphere_gap = 2.0-math.hypot(0.1, 0.5)-0.2-1e-6
    assert old_sphere_gap > 0
    assert gap(center, axis, 0.1, 0.5, start, end, 0.2, 1e-6) >= old_sphere_gap
    assert gap([0.0, 0.0, 0.0], axis, 0.1, 0.5, start, end, 0.2, 1e-6) == -math.inf
