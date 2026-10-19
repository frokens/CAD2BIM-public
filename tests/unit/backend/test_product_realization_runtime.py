from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import cadquery as cq
import pytest
from cadquery import exporters, importers
from pydantic import ValidationError

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.application.product_realization_input_service import (
    normalize_product_realization_invocation,
)
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.ifc_geometry_basis import IfcLengthUnit
from CAD2BIM_backend.domain.ifc_geometry_construction import IfcGeometryRepresentationRoute
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometryFormat,
    GeometryNormalizeRequest,
    GeometryRuntimeResourceLimitError,
    GeometryRuntimeUnavailableError,
    GeometryRuntimeValidationError,
    GeometrySourceFormat,
    GeometryBuildInput,
)
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyComponentContract,
    ProductFamilyContract,
    ProductFamilyGeometryFragmentContract,
    ProductFamilyFragmentBindingOption,
    ProductFamilyIfcClass,
    ProductFamilyNumberParameterContract,
    ProductFamilyStructureKind,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationInvocation,
    ProductRealizationOutputNeed,
    ProductRealizationRequest,
    ProductRealizationStatus,
    ProductRealizationValidationScenario,
    ResolvedMappedGeometryBinding,
    ResolvedModelingFunctionBinding,
    RigidTransform3D,
    rigid_transform_from_axis_reference,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.codec import normalize_geometry
from CAD2BIM_backend.infrastructure.geometry_runtime.codec_execution import (
    GEOMETRY_CODEC_BUDGET,
    OcpGeometryCodecExecutor,
)
from CAD2BIM_backend.infrastructure.product_realization_runtime import (
    OcpCadQueryProductRealizationRuntime,
)
from CAD2BIM_backend.infrastructure.product_realization_runtime import (
    protocol as realization_protocol,
)
from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle
from CAD2BIM_backend.infrastructure.worker_runtime.python_bundle import (
    PythonBundleValidationError,
    compile_python_bundle,
)
from CAD2BIM_backend.infrastructure.worker_runtime import (
    HostWorkerCapacity,
    WorkerLaunchSpec,
    WorkerProcessBudget,
    WorkerProcessObservations,
    WorkerProcessSupervisor,
    WorkerSupervisorError,
    WorkerTerminal,
)
from CAD2BIM_backend.ports.product_realization_runtime import (
    ProductRealizationRuntimeUnavailableError,
    ProductRealizationRuntimeResourceLimitError,
    ProductRealizationRuntimeValidationError,
)

from tests.unit.backend.product_family_fixtures import (
    current_family_contract,
    current_model_interface,
)


_CANONICAL_REOPEN_FIXTURES = (
    Path(__file__).resolve().parents[2] / "fixtures" / "geometry_runtime" / "canonical_reopen"
)
_ATTEMPT_04_FIXTURE = _CANONICAL_REOPEN_FIXTURES / "attempt_04"


def test_numeric_worker_module_does_not_load_native_dependencies():
    source_root = Path(__file__).resolve().parents[3] / "src"
    code = (
        "import sys; sys.path.insert(0, " + repr(str(source_root)) + "); "
        "import CAD2BIM_backend.infrastructure.product_realization_runtime.child as child; "
        "child._require_modeling_environment(native=False); "
        "from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingArena; "
        "arena = ModelingArena(profile='family_numeric'); "
        "assert not any(n == 'cadquery' or n == 'OCP' or n.startswith('OCP.') for n in sys.modules); "
        "arena.close()"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("helper", [
    "from .main import build\ndef value():\n    return 1\n",
    "import cadquery\ndef value():\n    return 1\n",
    "from .bridge import value\n",
])
def test_geometry_interfaces_reject_native_or_indirect_main_helpers(helper):
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import compile_geometry_interfaces
    content = pack_source_bundle({
        "main.py": b"import cadquery as cq\ndef build(ctx):\n    return cq.Workplane().box(1, 1, 1)\n",
        "interfaces.py": b"from .helper import value\ndef evaluate(ctx):\n    value()\n",
        "helper.py": helper.encode(),
        "bridge.py": b"from .main import build\ndef value():\n    return 1\n",
    })
    with pytest.raises(PythonBundleValidationError):
        compile_geometry_interfaces(content, "cad2bim_test_interfaces")


@pytest.mark.asyncio
async def test_numeric_precheck_retains_normal_fail_without_brep_then_continues(tmp_path):
    from CAD2BIM_backend.domain.product_realization import validate_product_realization_result_integrity
    failing = _model_interface_pair_request(tmp_path,
        right_frame={"translation_m": (0.1, 0, 0)}, relation={"direction_relation": "same", "orientation_requirement": "full_frame", "profile_requirement": "equal"})
    binding = failing.geometry_bindings[0].model_copy(update={"geometry": None})
    failing = failing.model_copy(update={"geometry_bindings": (binding,)})
    # Same exact mapped closure must remain invariant in one numeric batch.
    passing = failing.model_copy(update={"invocation": failing.invocation.model_copy(update={"product_origin": (4., 0., 0.)})})
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    results = [item async for item in runtime.precheck_batch((failing, passing))]
    assert len(results) == 2
    for request, result in zip((failing, passing), results, strict=True):
        assert result.execution_stage == "interface_constraints_evaluated"
        assert result.frame_check_status == "FAIL"
        assert result.instance_receipts == ()
        assert result.interface_evaluations
        restored = type(result).model_validate_json(result.model_dump_json())
        assert restored.execution_stage == "interface_constraints_evaluated"
        assert restored.frame_check_status == "FAIL"
        validate_product_realization_result_integrity(request, restored)


@pytest.mark.asyncio
async def test_precheck_then_instantiation_uses_saved_proposal_and_local_brep(tmp_path):
    import psutil
    from CAD2BIM_backend.domain.runtime_execution import PRODUCT_REALIZATION_EXECUTION
    request = _request(_mapped_binding(tmp_path))
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    registrations = []
    async def register_guard(pid, birth_us):
        guard = psutil.Process(pid)
        assert round(guard.create_time() * 1_000_000) == birth_us
        assert guard.children() == []  # Guardian still holds the Worker start gate.
        if registrations:
            assert not psutil.pid_exists(registrations[-1][0])
        registrations.append((pid, birth_us))
    async with runtime.invocation_scope(PRODUCT_REALIZATION_EXECUTION, on_guard_registered=register_guard):
        checks = [item async for item in runtime.precheck_batch((request,))]
        outputs = [item async for item in runtime.instantiate_batch((request,), checks)]
    assert checks[0].execution_stage == "interface_constraints_evaluated"
    assert checks[0].frame_check_status == "PASS"
    assert len(registrations) == 3  # Numeric, instantiation, then independent Codec.
    assert outputs[0].realization.proposal == checks[0].proposal
    assert outputs[0].realization.execution_stage == "instantiated"
    assert outputs[0].realization.interface_project_world_frames == checks[0].interface_project_world_frames
    assert outputs[0].geometry is None
    assert set(outputs[0].instance_geometries) == {item.fragment_key for item in checks[0].proposal.fragments}


@pytest.mark.asyncio
async def test_instantiation_rejects_changed_frozen_invocation_and_policy_before_worker(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from CAD2BIM_backend.domain.product_realization import ProductRealizationExternalConstraint
    request = _request(_mapped_binding(tmp_path))
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    checks = [result async for result in runtime.precheck_batch((request,))]
    changed_family = current_family_contract(
        interfaces=tuple({"interface_key": key, "owner_component_key": "body", "interface_kind": "internal",
            "source": {"source_kind": "model_interface", "fragment_key": "body_shape", "model_interface_key": "datum"}}
            for key in ("a", "b")),
        constraints=({"constraint_key": "same_datum", "constraint_kind": "interface_coincidence",
            "interface_a_key": "a", "interface_b_key": "b", "position_tolerance_m": 1e-6,
            "angular_tolerance_rad": 1e-6,
            "frame_relation": {"direction_relation": "same", "orientation_requirement": "full_frame", "profile_requirement": "equal"}},))
    changed_condition = ProductRealizationExternalConstraint.model_validate({
        "interface_key": "datum", "source_kind": "design_target",
        "condition": {"kind": "fixed_point", "point_m": [1, 0, 0], "direction_relation": "none"}})
    changes = (
        {"invocation": request.invocation.model_copy(update={"product_origin": (1., 0., 0.)})},
        {"invocation": request.invocation.model_copy(update={"length_unit": IfcLengthUnit(prefix="MILLI")})},
        {"invocation": request.invocation.model_copy(update={"external_constraints": (changed_condition,)})},
        {"invocation": request.invocation.model_copy(update={"realization_kind": "standard_model"})},
        {"policy": request.policy.model_copy(update={"mode": "changed_policy"})},
        {"family_contract": changed_family, "family_contract_digest": sha256_digest(changed_family)},
    )
    forbidden = AsyncMock(side_effect=AssertionError("changed invocation reached Worker"))
    monkeypatch.setattr(runtime._supervisor, "run", forbidden)
    for update in changes:
        with pytest.raises(ProductRealizationRuntimeValidationError, match="request association"):
            async for _ in runtime.instantiate_batch((request.model_copy(update=update),), checks):
                pytest.fail("changed invocation produced geometry")
    forbidden.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["invocation", "precheck_proposal"])
async def test_instantiation_child_rejects_changed_invocation_frame(tmp_path, monkeypatch, tamper):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import adapter as module
    request = _request(_mapped_binding(tmp_path))
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    checks = [result async for result in runtime.precheck_batch((request,))]
    original = module.batch_invocation_frame
    def tampered_frame(index, request, **kwargs):
        frame = original(index, request, **kwargs)
        if "precheck" in frame:
            if tamper == "invocation":
                frame["invocation"]["product_origin"] = [1., 0., 0.]
            else:
                frame["precheck"]["proposal"]["fragments"][0]["binding_key"] = "other_geometry"
        return frame
    monkeypatch.setattr(module, "batch_invocation_frame", tampered_frame)
    with pytest.raises(ProductRealizationRuntimeValidationError) as captured:
        async for _ in runtime.instantiate_batch((request,), checks):
            pytest.fail("IPC mismatch produced geometry")
    assert captured.value.phase == "input_validation"
    assert captured.value.item_index == 0


@pytest.mark.asyncio
async def test_numeric_precheck_technical_failure_preserves_prefix_and_stops(tmp_path):
    source = pack_source_bundle({"main.py": b"""def realize(ctx):
    if ctx.product_origin()[0] > 0:
        raise ValueError('author_program_failed')
    return {'status': 'SOLVED', 'components': [{'component_key': 'body'}],
            'fragments': [{'fragment_key': 'body_shape', 'binding_key': 'body_geometry'}]}
"""})
    request = _request(ResolvedMappedGeometryBinding(binding_key="body_geometry", geometry_content_digest="a" * 64), source=source)
    second = request.model_copy(update={"invocation": request.invocation.model_copy(update={"product_origin": (1., 0., 0.)})})
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    completed = []
    with pytest.raises(ProductRealizationRuntimeValidationError) as captured:
        async for result in runtime.precheck_batch((request, second, request)):
            completed.append(result)
    assert len(completed) == 1
    assert captured.value.item_index == 1
    assert captured.value.phase == "execution"
    assert completed[0].execution_stage == "interface_constraints_evaluated"


@pytest.mark.asyncio
async def test_numeric_proposal_contract_rejection_is_normal_and_continues(tmp_path):
    source = pack_source_bundle({"main.py": b"""def realize(ctx):
    components = [{'component_key': 'body'}]
    if ctx.product_origin()[0] < 0:
        components.append({'component_key': 'body'})
    return {'status': 'SOLVED', 'components': components,
            'fragments': [{'fragment_key': 'body_shape', 'binding_key': 'body_geometry'}]}
"""})
    request = _request(ResolvedMappedGeometryBinding(binding_key="body_geometry", geometry_content_digest="a" * 64), source=source)
    first = request.model_copy(update={"invocation": request.invocation.model_copy(update={"product_origin": (-1., 0., 0.)})})
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    results = [result async for result in runtime.precheck_batch((first, request))]
    assert len(results) == 2
    assert results[0].proposal.status is ProductRealizationStatus.CONTRACT_REJECTED
    assert results[0].proposal.rejection.violation_key == "proposal_contract_rejected"
    assert results[0].execution_stage == "solved"
    assert results[0].interface_evaluations == ()
    assert results[1].frame_check_status == "PASS"


@pytest.mark.asyncio
async def test_function_interface_precheck_never_loads_or_compiles_main(tmp_path):
    source = pack_source_bundle({
        "main.py": b"raise RuntimeError('build_must_not_be_loaded')\n",
        "interfaces.py": b"def evaluate(ctx):\n    ctx.define_interface(key='datum', origin_m=(0,0,0), axis=(0,0,1), reference_direction=(1,0,0))\n",
    })
    binding = ResolvedModelingFunctionBinding(binding_key="body_geometry", geometry_content_digest="b" * 64,
        source_bundle=source, source_bundle_sha256=hashlib.sha256(source).hexdigest(),
        parameter_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False})
    request = _request(binding)
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    results = [result async for result in runtime.precheck_batch((request,))]
    assert results[0].execution_stage == "interface_constraints_evaluated"
    assert results[0].frame_check_status == "PASS"
    assert results[0].instance_receipts == ()
    assert results[0].interface_evaluations[0].interfaces[0].interface_key == "datum"


def _evaluate_current_invocation(request, program, context, *, output_need):
    """Owner units exercise the same current precheck then instantiation contract."""
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child
    if output_need is ProductRealizationOutputNeed.SOLVE_ONLY:
        return child._evaluate_invocation(request, program, context, output_need=output_need)
    checked, _ = child._evaluate_invocation(
        request, program, context, output_need=ProductRealizationOutputNeed.INTERFACES)
    if (output_need is ProductRealizationOutputNeed.INTERFACES
            or checked.proposal.status is not ProductRealizationStatus.SOLVED
            or checked.frame_check_status != "PASS"):
        return checked, None
    if not hasattr(child, "read_geometry_payload"):
        child._load_geometry_dependencies()
    return child._evaluate_invocation(request, program, context, output_need=output_need, precheck=checked)

def _self_geometry_pipe_request(
    *,
    standard=False,
    wrong_parameter=False,
    points_m=None,
    diameter_m=0.1,
    product_origin=None,
    external_point_m=None,
    external_direction=None,
):
    fixture = _CANONICAL_REOPEN_FIXTURES.parent / "multipoint_pipe"
    geometry_source = pack_source_bundle({p.name: p.read_bytes() for p in fixture.glob("*.py")})
    schema = json.loads((fixture / "parameter_schema.json").read_text())
    binding = ResolvedModelingFunctionBinding(
        binding_key="pipe",
        geometry_content_digest="4" * 64,
        source_bundle=geometry_source,
        source_bundle_sha256=hashlib.sha256(geometry_source).hexdigest(),
        parameter_schema=schema,
    )
    coordinate = {
        "parameter_kind": "number",
        "unit": "metre",
        "minimum": -1000,
        "maximum": 1000,
    }
    family = ProductFamilyContract.model_validate(
        {
            "product_structure_kind": "single_product",
            "public_parameters": [
                {
                    "parameter_key": "points_m",
                    "parameter_kind": "array",
                    "min_items": 2,
                    "max_items": 16,
                    "items": {

                        "parameter_kind": "array",
                        "min_items": 3,
                        "max_items": 3,
                        "items": coordinate,
                    },
                }
            ],
            "solved_parameters": [
                {
                    "parameter_key": "diameter_m",
                    "parameter_kind": "number",
                    "unit": "metre",
                    "minimum": 0.05,
                    "maximum": 0.2,
                }
            ],
            "components": [{"component_key": "body", "ifc_class": "IfcPipeSegment"}],
            "geometry_fragments": [
                {
                    "fragment_key": "shape",
                    "owner_component_key": "body",
                    "binding_options": [{"binding_key": "pipe", "parameter_sources": {"overrides": {
                        "points_m": {
                            "source_kind": "public_parameter",
                            "parameter_key": "points_m",
                        },
                        "outer_diameter_m": {
                            "source_kind": "solved_parameter",
                            "parameter_key": "diameter_m",
                        },
                        "section": {
                            "source_kind": "fixed",
                            "value": {"mode": "wall_thickness", "wall_thickness_m": 0.01},
                        },
                    }}}],
                }
            ],
            "interfaces": [
                {
                    "interface_key": "outlet",
                    "owner_component_key": "body",
                    "interface_kind": "external",
                    "source": {
                        "source_kind": "model_interface",
                        "fragment_key": "shape",
                        "model_interface_key": "outlet",
                    },
                    "external_constraint": {
                        "allowed_kinds": ["fixed_point", "finite_segment", "infinite_axis"],
                        "allowed_directions": ["same"],
                        "maximum_position_tolerance_m": 1e-6,
                        "maximum_angular_tolerance_rad": 1e-6,
                    },
                }
            ],
            "standard_model": {},
        }
    )
    points_m = points_m or [[1, 2, 3], [1, 2, 4], [1, 3, 4], [2, 3, 4]]
    product_origin = product_origin or (10.0, 20.0, 30.0)
    external_point_m = external_point_m or [12, 23, 34]
    external_direction = external_direction or [1, 0, 0]
    invocation = ProductRealizationInvocation.model_validate(
        {
            "realization_kind": "standard_model" if standard else "self_geometry",
            "public_parameters": {"points_m": points_m},
            "product_origin": product_origin,
            "length_unit": {},
            "external_constraints": []
            if standard
            else [
                {
                    "interface_key": "outlet",
                    "source_kind": "design_target",
                    "condition": {
                        "kind": "fixed_point", "point_m": external_point_m,
                        "axis": external_direction, "direction_relation": "same",
                        "position_tolerance_m": 1e-6,
                        "angular_tolerance_rad": 1e-6,
                    },
                }
            ],
        }
    )
    invocation = normalize_product_realization_invocation(family, invocation)
    program = (
        """
def construct(ctx, standard):
    points = ctx.param("points_m")
    corrupted = ctx.parameters()
    corrupted["points_m"][0][0] = 999
    assert ctx.param("points_m")[0][0] == FIRST_COORD
    conditions = ctx.external_constraints()
    if conditions:
        conditions[0]["condition"]["point_m"] = (999, 999, 999)
        assert ctx.external_constraints()[0]["condition"]["point_m"] == TARGET_POINT
    return {"status": "SOLVED", "internal_parameters": {"diameter_m": SOLVED_DIAMETER},
        "components": [{"component_key": "body"}],
        "fragments": [{"fragment_key": "shape", "binding_key": "pipe"}]}
def realize(ctx):
    return construct(ctx, False)
def standard_model(ctx):
    return construct(ctx, True)
""".replace('"binding_key": "pipe"', '"binding_key": "pipe", "geometry_parameters": {"diameter_m": 0.12}' if wrong_parameter else '"binding_key": "pipe"')
        .replace("SOLVED_DIAMETER", repr(diameter_m))
        .replace("FIRST_COORD", repr(points_m[0][0]))
        .replace("TARGET_POINT", repr(tuple(float(value) for value in external_point_m)))
    )
    source = pack_source_bundle({"main.py": program.encode()})
    return ProductRealizationRequest(
        family_contract=family,
        family_contract_digest=sha256_digest(family),
        source_bundle=source,
        source_bundle_sha256=hashlib.sha256(source).hexdigest(),
        geometry_bindings=(binding,),
        invocation=invocation,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("standard", [False, True])
async def test_self_geometry_pipe_builds_after_numeric_solution_and_places_world_end_sections(
    tmp_path, standard
):
    from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import reopen_user_shape

    request = _self_geometry_pipe_request(standard=standard)
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        execution = await runtime.realize_geometry(request, include_instances=True)
    finally:
        await runtime.close()
    result = execution.realization
    assert result.frame_check_status == "PASS", result.model_dump(mode="json")
    assert result.component_project_world_transforms[0].project_world_transform.translation_m == request.invocation.product_origin
    assert (
        result.instance_receipts[0].normalized_parameters["points_m"]
        == request.invocation.public_parameters["points_m"]
    )
    shape = reopen_user_shape(execution.geometry.payload)
    assert shape.isValid() and len(shape.Solids()) == 1
    # CQ's miter sweep carries OCCT approximation tolerances. This integral
    # witness uses the existing multipoint 1 ppm volume tolerance; actual end
    # positions/directions below retain their independent 0.1 micrometre checks.
    assert shape.Volume() == pytest.approx(3 * 3.141592653589793 * (0.05**2 - 0.04**2), rel=1e-6)
    for expected_center, expected_axis in (((11, 22, 33), (0, 0, -1)), ((12, 23, 34), (1, 0, 0))):
        faces = [
            face
            for face in shape.Faces()
            if face.geomType() == "PLANE"
            and (face.Center() - cq.Vector(*expected_center)).Length < 1e-7
        ]
        assert len(faces) == 1
        assert faces[0].normalAt().toTuple() == pytest.approx(expected_axis, abs=1e-7)
        assert len(faces[0].Wires()) == 2
        assert faces[0].Area() == pytest.approx(3.141592653589793 * (0.05**2 - 0.04**2), rel=1e-8)
    receipt = result.instance_receipts[0]
    local = execution.instance_geometries[receipt.fragment_key]
    assert local.sha256 == receipt.geometry_sha256
    assert local.sha256 != execution.geometry.sha256
    assert result.interface_project_world_frames[
        0
    ].project_world_frame.translation_m == pytest.approx((12, 23, 34))
    if not standard:
        assert result.external_condition_checks[0].evaluation.satisfied
        assert (
            result.external_condition_checks[0].external_actual_connection_status == "NOT_CHECKED"
        )


# Private drawing-specific regression omitted from public distribution.


def _current_pipe_installation(source_facts):
    points = source_facts["world_points_m"]
    targets = []
    for key, point, adjacent in (("inlet", points[0], points[1]),
                                 ("outlet", points[-1], points[-2])):
        axis = [adjacent[index]-point[index] for index in range(3)]
        targets.append({"interface_key": key, "condition": {
            "kind": "fixed_point", "point_m": point,
            "axis": axis if math.hypot(*axis) else [1, 0, 0],
            "outer_diameter_m": source_facts["outer_diameter_m"],
            "direction_relation": "opposed",
        }})
    return {"middle_points_m": points[1:-1], "section": source_facts["section"]}, targets


def _current_pipe_numeric(source_facts, origin):
    from CAD2BIM_backend.infrastructure.product_realization_runtime.child import _compile_family
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_family_program
    root = Path(__file__).resolve().parents[2] / "scenarios/engineering_product_families"
    manifest = json.loads((root / "families/pipe/manifest.json").read_text())
    source = pack_source_bundle({item["bundle_path"]: (root / item["repo_path"]).read_bytes()
                                for item in manifest["family"]["source_files"]})
    compiled = _compile_family(source, "current_pipe_numeric", "realize", required_entry_names=("realize", "standard_model"))
    parameters, targets = _current_pipe_installation(source_facts)

    class Context:
        def contract(self):
            return {}

        def policy(self):
            return {}

        def parameters(self):
            return parameters

        def param(self, key):
            return parameters[key]

        def product_origin(self):
            return origin

        def external_constraints(self):
            return targets

    return execute_family_program(compiled, Context(), entry_name="realize")


@pytest.mark.parametrize("row", json.loads(
    (_CANONICAL_REOPEN_FIXTURES.parent / "pipe_finite_miter_cases.json").read_text()
), ids=lambda row: row["product_key"].split(":")[-1][:8])
def test_current_pipe_finite_miter_original_inputs(row):
    result = _current_pipe_numeric(row["public_parameters"], row["product_origin"])
    if row["original_status"] == "INFEASIBLE":
        assert result["status"] == "INFEASIBLE"
        assert result["violations"][0]["violation_key"] == "miter_room"
    else:
        assert result["status"] == "SOLVED"
        from tests.scenarios.engineering_product_families.shared.local_geometry_frame import point_in_parent
        placement = result["fragments"][0]["local_transform"]
        for point, expected in zip(result["internal_parameters"]["local_points_m"], row["public_parameters"]["world_points_m"]):
            assert point_in_parent(point, placement) == pytest.approx(
                [expected[i]-row["product_origin"][i] for i in range(3)], abs=1e-9)


@pytest.mark.parametrize("points,status,key", [
    # A returning solid enters the first straight tube.
    ([[0, 0, 0], [3, 0, 0], [3, 1, 0], [.2, 1, 0], [.2, .05, 0]],
     "AMBIGUOUS", "body_overlap_unverified"),
    # Raw finite axes are more than OD apart, but the first oblique cap
    # extends to material (.4, -.08, .05) inside the last vertical tube.
    ([[-2, 0, 0], [0, 0, 0], [-3, 1, 0], [-3, 1, 3], [.4, -.08, 3], [.4, -.08, .02]],
     "AMBIGUOUS", "body_overlap_unverified"),
    ([[0, 0, 0], [3, 0, 0], [3, 3, 0], [1, -1, 0]], "INFEASIBLE", "self_intersection"),
    ([[0, 0, 0], [1, 0, 0], [0, 0, 0]], "INFEASIBLE", "foldback"),
    ([[0, 0, 0], [0, 0, 0], [1, 0, 0]], "INFEASIBLE", "zero_segment"),
])
def test_current_pipe_finite_miter_keeps_real_overlap_and_path_boundaries(points, status, key):
    result = _current_pipe_numeric({"world_points_m": points, "outer_diameter_m": .2,
                                    "section": {"known_wall_thickness_m": []}}, [0, 0, 0])
    assert result["status"] == status
    if status == "INFEASIBLE":
        assert result["violations"][0]["violation_key"] == key
    else:
        assert result["ambiguities"][0]["ambiguity_key"] == key


def test_current_pipe_finite_miter_unresolved_world_subtraction_is_not_separation():
    result = _current_pipe_numeric({
        "world_points_m": [[0, 0, 0], [3, 0, 0], [3, 1, 0], [6, 1, 0]],
        "outer_diameter_m": .2, "section": {"known_wall_thickness_m": []},
    }, [1e16, 1e16, 1e16])
    assert result["status"] == "AMBIGUOUS"
    assert result["ambiguities"][0]["ambiguity_key"] == "body_overlap_unverified"


def test_current_pipe_finite_miter_unresolved_cap_denominator_is_not_separation():
    # Long, almost reversed directions leave a nominal positive miter gap,
    # but the normalized direction error cannot resolve the cap denominator.
    result = _current_pipe_numeric({
        "world_points_m": [[0, 0, 0], [1e7, 0, 0], [-1e7, 1, 0], [-2e7, 1, 0]],
        "outer_diameter_m": .02, "section": {"known_wall_thickness_m": []},
    }, [0, 0, 0])
    assert result["status"] == "AMBIGUOUS"
    assert result["ambiguities"][0]["ambiguity_key"] == "body_overlap_unverified"


# Private drawing-specific regression omitted from public distribution.


# Private drawing-specific regression omitted from public distribution.


@pytest.mark.asyncio
async def test_self_geometry_runtime_rejects_retired_author_geometry_parameters(
    tmp_path,
):
    request = _self_geometry_pipe_request(wrong_parameter=True)
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        execution = await runtime.realize_geometry(request)
        assert execution.realization.proposal.status is ProductRealizationStatus.CONTRACT_REJECTED
        assert "geometry_parameters" in execution.realization.proposal.rejection.message
        assert execution.geometry is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_readiness_uses_current_author_contract_in_real_child(tmp_path, monkeypatch):
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "readiness-capacity")
    original = runtime.realize
    results = []
    async def checked(request):
        result = await original(request)
        results.append(result)
        return result
    monkeypatch.setattr(runtime, "realize", checked)
    try:
        await runtime.verify_ready()
        assert len(results) == 1
        assert results[0].proposal.fragments[0].binding_key == "probe_geometry"
        assert results[0].instance_receipts[0].normalized_parameters == {}
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_readiness_uses_current_author_contract_in_real_child(tmp_path, monkeypatch):
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "readiness-capacity")
    original = runtime.realize
    results = []
    async def checked(request):
        result = await original(request)
        results.append(result)
        return result
    monkeypatch.setattr(runtime, "realize", checked)
    try:
        await runtime.verify_ready()
        assert len(results) == 1
        assert "abi_profile" not in results[0].model_dump()
        assert results[0].proposal.fragments[0].geometry_parameters == {}
        assert results[0].instance_receipts[0].normalized_parameters == {}
    finally:
        await runtime.close()


def test_parent_adapter_import_does_not_load_native_geometry_codec() -> None:
    source_root = Path(__file__).resolve().parents[3] / "src"
    script = """
import sys
sys.path.insert(0, sys.argv[1])
import CAD2BIM_backend.infrastructure.product_realization_runtime.adapter
forbidden = [
    name for name in sys.modules
    if name == "cadquery"
    or name.startswith("cadquery.")
    or name == "OCP"
    or name.startswith("OCP.")
    or name == "CAD2BIM_backend.infrastructure.geometry_runtime.codec"
]
if forbidden:
    raise SystemExit("unexpected native parent imports: " + ",".join(sorted(forbidden)))
"""

    completed = subprocess.run(
        (sys.executable, "-I", "-B", "-c", script, str(source_root)),
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr


def _family() -> ProductFamilyContract:
    return current_family_contract()


def _mapped_binding(tmp_path: Path) -> ResolvedMappedGeometryBinding:
    path = tmp_path / "body.brep"
    exporters.export(
        cq.Workplane("XY").box(1.0, 0.5, 0.25).val(),
        str(path),
        exportType="BREP",
    )
    geometry = normalize_geometry(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            source_format=GeometrySourceFormat.OCCT_BREP,
            payload=path.read_bytes(),
        )
    )
    return ResolvedMappedGeometryBinding(
        binding_key="body_geometry",
        geometry_content_digest=geometry.sha256,
        geometry=geometry,
        interfaces=(current_model_interface(),),
    )


def _program(*, entry: str = "realize") -> bytes:
    return pack_source_bundle(
        {
            "main.py": (
                b"from .layout import identity\n\n"
                + f"def {entry}(ctx):\n".encode()
                + b"    return {\n"
                + b"        'status': 'SOLVED',\n"
                + b"        'components': [{'component_key': 'body'}],\n"
                + b"        'fragments': [{\n"
                + b"            'fragment_key': 'body_shape',\n"
                + b"            'binding_key': 'body_geometry',\n"
                + b"""
"""
                + b"            'local_transform': identity(),\n"
                + b"        }],\n"
                + b"    }\n"
            ),
            "layout.py": (
                b"def identity():\n"
                b"    return {\n"
                b"        'translation_m': [0.0, 0.0, 0.0],\n"
                b"        'rotation': [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], "
                b"[0.0, 0.0, 1.0]],\n"
                b"    }\n"
            ),
        }
    )


def _request(
    binding: object,
    *,
    source: bytes | None = None,
    invocation: ProductRealizationInvocation | None = None,
    parameter_sources: dict | None = None,
) -> ProductRealizationRequest:
    family = current_family_contract(parameter_sources=parameter_sources)
    source_bundle = source or _program()
    return ProductRealizationRequest(
        family_contract=family,
        family_contract_digest=sha256_digest(family),
        source_bundle=source_bundle,
        source_bundle_sha256=hashlib.sha256(source_bundle).hexdigest(),
        geometry_bindings=(binding,),
        invocation=invocation or ProductRealizationInvocation(
            product_origin=(0.0, 0.0, 0.0), length_unit=IfcLengthUnit(),
        ),
    )


def _family_request(
    family: ProductFamilyContract,
    binding: object,
    source: bytes,
    *,
    invocation: ProductRealizationInvocation | None = None,
) -> ProductRealizationRequest:
    return ProductRealizationRequest(
        family_contract=family,
        family_contract_digest=sha256_digest(family),
        source_bundle=source,
        source_bundle_sha256=hashlib.sha256(source).hexdigest(),
        geometry_bindings=(binding,),
        invocation=invocation or ProductRealizationInvocation(
            product_origin=(0.0, 0.0, 0.0), length_unit=IfcLengthUnit(),
        ),
    )


def _mixed_numeric_request(
    tmp_path: Path,
    *,
    product_origin: tuple[float, float, float] = (10.0, 20.0, 30.0),
) -> ProductRealizationRequest:
    mapped_base = _mapped_binding(tmp_path)
    mapped = mapped_base.model_copy(
        update={
            "binding_key": "mapped_geometry",
            "interfaces": (
                current_model_interface(
                    key="joint_mapped",
                    profile={"kind": "circular", "diameter_m": 0.2},
                ),
            ),
        }
    )
    function_source = pack_source_bundle(
        {'main.py': b"import cadquery as cq\n\ndef build(ctx):\n    length = ctx.param('length_m')\n    width = ctx.param('width_m')\n    return cq.Workplane('XY').box(length, width, width)\n",
'interfaces.py': b"def evaluate(ctx):\n    length = ctx.param('length_m')\n    ctx.define_interface(key='joint_function', origin_m=(0, 0, 0), axis=(0, 0, 1), reference_direction=(1, 0, 0), profile={'kind': 'circular', 'diameter_m': 0.4})\n    ctx.define_interface(key='outlet', origin_m=(length, 0, 0), axis=(1, 0, 0), reference_direction=(0, 0, 1), profile={'kind': 'circular', 'diameter_m': 0.2})\n"}
    )
    function = ResolvedModelingFunctionBinding(
        binding_key="function_geometry",
        geometry_content_digest=hashlib.sha256(function_source).hexdigest(),
        source_bundle=function_source,
        source_bundle_sha256=hashlib.sha256(function_source).hexdigest(),
        parameter_schema={
            "type": "object",
            "properties": {
                "length_m": {"type": "number", "minimum": 0.01, "maximum": 2.0},
                "width_m": {"type": "number", "minimum": 0.01, "maximum": 1.0},
            },
            "required": ["length_m", "width_m"],
            "additionalProperties": False,
        },
    )
    family = ProductFamilyContract.model_validate(
        {
            "product_structure_kind": "semantic_assembly",
            "components": [
                {"component_key": "assembly", "ifc_class": "IfcElementAssembly"},
                {
                    "component_key": "body",
                    "ifc_class": "IfcValve",
                    "parent_component_key": "assembly",
                    "local_transform_mode": "program",
                },
            ],
            "geometry_fragments": [
                {
                    "fragment_key": "mapped_primary",
                    "owner_component_key": "body",
                    "binding_options": [{"binding_key": "mapped_geometry", "parameter_sources": {"overrides": {}},
                                         "uniform_scale_source": {"source_kind": "fixed", "value": 2.0}}],
                    "local_transform_mode": "program",
                },
                {
                    "fragment_key": "mapped_secondary",
                    "owner_component_key": "body",
                    "binding_options": [{"binding_key": "mapped_geometry", "parameter_sources": {"overrides": {}}}],
                    "fixed_local_transform": {"translation_m": [0.0, 2.0, 0.0]},
                },
                {
                    "fragment_key": "function_shape",
                    "owner_component_key": "body",
                    "binding_options": [{"binding_key": "function_geometry", "parameter_sources": {"overrides": {
                        "length_m": {"source_kind": "fixed", "value": 0.4},
                        "width_m": {"source_kind": "fixed", "value": 0.2},
                    }}}],
                    "local_transform_mode": "program",
                },
            ],
            "interfaces": [
                {
                    "interface_key": "mapped_joint",
                    "owner_component_key": "body",
                    "interface_kind": "internal",
                    "source": {
                        "source_kind": "model_interface",
                        "fragment_key": "mapped_primary",
                        "model_interface_key": "joint_mapped",
                    },
                },
                {
                    "interface_key": "function_joint",
                    "owner_component_key": "body",
                    "interface_kind": "internal",
                    "source": {
                        "source_kind": "model_interface",
                        "fragment_key": "function_shape",
                        "model_interface_key": "joint_function",
                    },
                },
                {
                    "interface_key": "outlet",
                    "owner_component_key": "body",
                    "interface_kind": "external",
                    "source": {
                        "source_kind": "model_interface",
                        "fragment_key": "function_shape",
                        "model_interface_key": "outlet",
                    },
                    "connection_profile": {
                        "profile_kind": "circular",
                        "outer_diameter_m": 0.2,
                    },
                    "external_constraint": {
                        "allowed_kinds": ["fixed_point"],
                        "allowed_directions": ["same"],
                        "maximum_position_tolerance_m": 1e-6,
                        "maximum_angular_tolerance_rad": 1e-6,
                    },
                },
            ],
            "constraints": [
                {
                    "constraint_kind": "interface_coincidence",
                    "constraint_key": "actual_joint",
                    "interface_a_key": "mapped_joint",
                    "interface_b_key": "function_joint",
                    "position_tolerance_m": 1e-6,
                    "angular_tolerance_rad": 1e-6,
                    "frame_relation": {
                        "direction_relation": "same",
                        "orientation_requirement": "full_frame",
                        "profile_requirement": "equal",
                    },
                }
            ],
        }
    )
    family_source = pack_source_bundle(
        {
            "main.py": b"""import math

def rz90(x, y, z):
    angle = math.pi / 2.0
    c = math.cos(angle)
    s = math.sin(angle)
    return {'translation_m': [x, y, z],
            'rotation': [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]}

def realize(ctx):
    if len(ctx.product_origin()) != 3:
        raise ValueError('product origin is incomplete')
    contract = ctx.contract()
    if contract['product_structure_kind'] != 'semantic_assembly':
        raise ValueError('contract structure is unavailable')
    if contract['components'][0]['ifc_class'] != 'IfcElementAssembly':
        raise ValueError('contract component class is unavailable')
    contract['components'][0]['ifc_class'] = 'local-copy-only'
    contract['geometry_fragments'][2]['binding_options'][0]['parameter_sources']['overrides']['length_m']['value'] = 99.0
    fresh_contract = ctx.contract()
    if fresh_contract['components'][0]['ifc_class'] != 'IfcElementAssembly':
        raise ValueError('contract component snapshot leaked a mutation')
    function_parameters = fresh_contract['geometry_fragments'][2]['binding_options'][0]['parameter_sources']['overrides']
    if function_parameters['length_m']['value'] != 0.4:
        raise ValueError('contract parameter snapshot leaked a mutation')
    shared = rz90(1.0, 0.0, 0.0)
    return {
        'status': 'SOLVED',
        'components': [
            {'component_key': fresh_contract['components'][0]['component_key']},
            {'component_key': 'body', 'local_transform': rz90(2.0, 0.0, 0.0)},
        ],
        'fragments': [
            {'fragment_key': 'mapped_primary', 'binding_key': 'mapped_geometry',
             'local_transform': shared, 'uniform_scale': 2.0},
            {'fragment_key': 'mapped_secondary', 'binding_key': 'mapped_geometry'},
            {'fragment_key': 'function_shape', 'binding_key': 'function_geometry',

             'local_transform': shared},
        ],
    }
"""
        }
    )
    outlet_point = (
        product_origin[0] + 1.6,
        product_origin[1] + 1.0,
        product_origin[2],
    )
    invocation = ProductRealizationInvocation.model_validate(
        {
            "product_origin": product_origin,
            "length_unit": {},
            "external_constraints": [
                {
                    "interface_key": "outlet",
                    "source_kind": "design_target",
                    "condition": {
                        "kind": "fixed_point", "point_m": outlet_point,
                        "axis": [-1.0, 0.0, 0.0], "direction_relation": "same",
                        "outer_diameter_m": 0.2,
                        "position_tolerance_m": 1e-6,
                        "angular_tolerance_rad": 1e-6,
                    },
                }
            ],
        }
    )
    return ProductRealizationRequest(
        family_contract=family,
        family_contract_digest=sha256_digest(family),
        source_bundle=family_source,
        source_bundle_sha256=hashlib.sha256(family_source).hexdigest(),
        geometry_bindings=(mapped, function),
        invocation=invocation,
    )


def test_mixed_numeric_request_preflights_without_geometry(tmp_path: Path, monkeypatch) -> None:
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    request = _mixed_numeric_request(tmp_path)
    program = child._compile_family(request.source_bundle, "test_mixed_preflight", "realize")
    context = child._ProductRealizationContext(request)
    targets = context.external_constraints()
    assert targets[0]["condition"]["kind"] == "fixed_point"
    assert "position" not in targets[0]["condition"]
    targets[0]["condition"]["point_m"] = (999, 999, 999)
    assert context.external_constraints()[0]["condition"]["point_m"] == request.invocation.external_constraints[0].condition.point_m

    class ForbiddenPreparation:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("solve-only mixed proposal reached Geometry")

    monkeypatch.setattr(child, "_PreparedGeometryBindings", ForbiddenPreparation)
    result, preview = _evaluate_current_invocation(
        request,
        program,
        context,
        output_need=ProductRealizationOutputNeed.SOLVE_ONLY,
    )
    assert preview is None
    assert result.proposal.status is ProductRealizationStatus.SOLVED
    assert result.instance_receipts == ()
    assert result.component_project_world_transforms == ()
    assert sha256_digest(request.family_contract) == request.family_contract_digest


def test_gas_meter_short_targets_finish_in_existing_runtime_before_geometry(monkeypatch) -> None:
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child
    from tests.unit.scenarios.family_numeric_fixtures import current_numeric_request

    root = Path(__file__).resolve().parents[2] / "scenarios/engineering_product_families"
    family_root = root / "families/gas_meter"
    case = next(item for item in json.loads((family_root / "preview_cases.json").read_text())["cases"]["items"]
                if item["case_key"] == "true_short_window")
    invocation = case["invocation"]
    context = SimpleNamespace(
        parameters=lambda: invocation["public_parameters"],
        product_origin=lambda: invocation["product_origin"],
        external_constraints=lambda: invocation["external_constraints"],
    )
    request = current_numeric_request(root, "gas_meter", ProductFamilyContract.model_validate(
        json.loads((family_root / "definition.json").read_text())), context)
    program = child._compile_family(request.source_bundle, "meter_short_runtime", "realize")
    geometry_calls = []

    def forbidden_geometry(*_args, **_kwargs):
        geometry_calls.append("build_fragments")
        raise AssertionError("an infeasible installation must finish before Geometry")

    monkeypatch.setattr(child._ProductRealizationContext, "build_fragments", forbidden_geometry)
    result, preview = _evaluate_current_invocation(
        request, program, child._ProductRealizationContext(request),
        output_need=ProductRealizationOutputNeed.PREVIEW_WORLD,
    )
    assert result.proposal.status is ProductRealizationStatus.INFEASIBLE
    assert result.proposal.violations[0].violation_key == "finite_window"
    assert result.instance_receipts == ()
    assert result.interface_project_world_frames == ()
    assert preview is None
    assert geometry_calls == []


def test_fragment_binding_option_selects_only_its_sources_and_receipt(tmp_path: Path) -> None:
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    mapped = _mapped_binding(tmp_path)
    sphere_source = pack_source_bundle({'main.py': b"import cadquery as cq\ndef build(ctx):\n    return cq.Solid.makeSphere(ctx.param('radius_m'))\n",
'interfaces.py': b'def evaluate(ctx):\n    pass\n'})
    sphere = ResolvedModelingFunctionBinding(
        binding_key="sphere_geometry",
        geometry_content_digest=hashlib.sha256(sphere_source).hexdigest(),
        source_bundle=sphere_source,
        source_bundle_sha256=hashlib.sha256(sphere_source).hexdigest(),
        parameter_schema={
            "type": "object",
            "properties": {"radius_m": {"type": "number", "minimum": 0.01}},
            "required": ["radius_m"],
            "additionalProperties": False,
        },
    )
    family = current_family_contract(
        public_parameters=[{"parameter_key": "mode", "parameter_kind": "choice",
                            "choices": ["mapped", "sphere", "undeclared", "sphere_missing"]}],
        solved_parameters=[
            {"parameter_key": "scale", "parameter_kind": "number", "required": False,
             "minimum": 0.5, "maximum": 3.0},
            {"parameter_key": "radius", "parameter_kind": "number", "required": False,
             "unit": "metre", "minimum": 0.01, "maximum": 1.0},
        ],
        geometry_fragments=[{
            "fragment_key": "body_shape", "owner_component_key": "body",
            "binding_options": [
                {"binding_key": "body_geometry", "parameter_sources": {"overrides": {}},
                 "uniform_scale_source": {"source_kind": "solved_parameter", "parameter_key": "scale"}},
                {"binding_key": "sphere_geometry", "parameter_sources": {"overrides": {
                    "radius_m": {"source_kind": "solved_parameter", "parameter_key": "radius"}}}},
            ],
        }],
    )
    source = pack_source_bundle({"main.py": b"""def realize(ctx):
    mode = ctx.param('mode')
    selected = 'body_geometry' if mode == 'mapped' else 'sphere_geometry'
    if mode == 'undeclared':
        selected = 'other_geometry'
    solved = {'scale': 2.0} if mode == 'mapped' else {'radius': 0.2}
    if mode == 'sphere_missing' or mode == 'undeclared':
        solved = {}
    return {'status': 'SOLVED', 'internal_parameters': solved,
            'components': [{'component_key': 'body'}],
            'fragments': [{'fragment_key': 'body_shape', 'binding_key': selected,

                           'uniform_scale': 2.0 if mode == 'mapped' else 1.0}]}
"""})
    program = child._compile_family(source, "test_binding_options", "realize")

    def request(mode: str) -> ProductRealizationRequest:
        return ProductRealizationRequest(
            family_contract=family, family_contract_digest=sha256_digest(family),
            source_bundle=source, source_bundle_sha256=hashlib.sha256(source).hexdigest(),
            geometry_bindings=(mapped, sphere),
            invocation=ProductRealizationInvocation(
                public_parameters={"mode": mode}, product_origin=(0.0, 0.0, 0.0),
                length_unit=IfcLengthUnit()),
        )

    for mode, expected_binding in (("mapped", "body_geometry"), ("sphere", "sphere_geometry")):
        selected_request = request(mode)
        result, _ = _evaluate_current_invocation(
            selected_request, program,
            child._ProductRealizationContext(selected_request),
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
        )
        assert result.proposal.status is ProductRealizationStatus.SOLVED
        assert result.proposal.fragments[0].binding_key == expected_binding
        assert result.instance_receipts[0].binding_key == expected_binding

    for mode, expected_path in (
        ("undeclared", "geometry_fragments.0.binding_options"),
        ("sphere_missing", "geometry_fragments.0.binding_options.1.parameter_sources.overrides.radius_m"),
    ):
        invalid_request = request(mode)
        rejected, _ = _evaluate_current_invocation(
            invalid_request, program, child._ProductRealizationContext(invalid_request),
            output_need=ProductRealizationOutputNeed.SOLVE_ONLY,
        )
        assert rejected.proposal.status is ProductRealizationStatus.CONTRACT_REJECTED
        assert expected_path in rejected.proposal.rejection.message


def test_v3_flat_fragment_binding_is_rejected() -> None:
    legacy = _family().model_dump(mode="json", by_alias=True)
    legacy["schema"] = "product_family_contract_v3"
    fragment = legacy["geometry_fragments"][0]
    option = fragment.pop("binding_options")[0]
    fragment.update(option)

    with pytest.raises(ValidationError):
        ProductFamilyContract.model_validate(legacy)


def test_selected_geometry_binding_loads_lazily_and_reuses_batch_preparation(
    tmp_path: Path, monkeypatch,
) -> None:
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    mapped = _mapped_binding(tmp_path)
    function_source = pack_source_bundle({'main.py': b"import cadquery as cq\ndef build(ctx):\n    return cq.Workplane('XY').box(0.2, 0.2, 0.2)\n",
'interfaces.py': b'def evaluate(ctx):\n    pass\n'})
    function = ResolvedModelingFunctionBinding(
        binding_key="function_geometry",
        geometry_content_digest=hashlib.sha256(function_source).hexdigest(),
        source_bundle=function_source,
        source_bundle_sha256=hashlib.sha256(function_source).hexdigest(),
        parameter_schema={"type": "object", "properties": {}, "required": [],
                          "additionalProperties": False},
    )
    family = current_family_contract(
        public_parameters=[{"parameter_key": "choice", "parameter_kind": "choice",
                            "choices": ["mapped", "function"]}],
        geometry_fragments=[{
            "fragment_key": "body_shape", "owner_component_key": "body",
            "binding_options": [
                {"binding_key": "body_geometry", "parameter_sources": {"overrides": {}}},
                {"binding_key": "function_geometry", "parameter_sources": {"overrides": {}}},
            ],
        }],
    )
    source = pack_source_bundle({"main.py": b"""def realize(ctx):
    binding = 'body_geometry' if ctx.param('choice') == 'mapped' else 'function_geometry'
    return {'status': 'SOLVED', 'components': [{'component_key': 'body'}],
            'fragments': [{'fragment_key': 'body_shape', 'binding_key': binding}]}
"""})
    program = child._compile_family(source, "test_lazy_binding_options", "realize")
    bindings = (mapped, function)
    lifecycle = child._GeometryBindingLifecycle(bindings)
    selected = ""
    prepared: set[str] = set()
    child._load_geometry_dependencies()
    original_read = child.read_geometry_payload
    original_compile = child._compile_geometry

    def read_mapped(geometry):
        assert selected == "mapped" and "mapped" not in prepared
        prepared.add("mapped")
        return original_read(geometry)

    def compile_function(content, package_name, entry_name):
        assert selected == "function" and "function" not in prepared
        prepared.add("function")
        return original_compile(content, package_name, entry_name)

    monkeypatch.setattr(child, "read_geometry_payload", read_mapped)
    monkeypatch.setattr(child, "_compile_geometry", compile_function)

    def request(choice: str, exact_bindings=bindings) -> ProductRealizationRequest:
        return ProductRealizationRequest(
            family_contract=family, family_contract_digest=sha256_digest(family),
            source_bundle=source, source_bundle_sha256=hashlib.sha256(source).hexdigest(),
            geometry_bindings=exact_bindings,
            invocation=ProductRealizationInvocation(
                public_parameters={"choice": choice}, product_origin=(0.0, 0.0, 0.0),
                length_unit=IfcLengthUnit()),
        )

    for choice in ("mapped", "function", "mapped", "function"):
        selected = choice
        current = request(choice)
        result, _ = _evaluate_current_invocation(
            current, program, child._ProductRealizationContext(current, prepared=lifecycle),
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
        )
        assert result.instance_receipts[0].binding_key == (
            "body_geometry" if choice == "mapped" else "function_geometry")
    assert prepared == {"mapped", "function"}

    def fail_selected_template(_geometry):
        raise ValueError("selected mapped template could not be reopened")

    monkeypatch.setattr(child, "read_geometry_payload", fail_selected_template)
    invalid = request("mapped")
    with pytest.raises(child._ChildError, match="selected mapped template could not be reopened") as failure:
        _evaluate_current_invocation(
            invalid, program,
            child._ProductRealizationContext(
                invalid, prepared=child._GeometryBindingLifecycle(bindings)),
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
        )
    assert failure.value.code == "REALIZATION_OUTPUT_INVALID"

    malformed_source = pack_source_bundle({"main.py": b"def build(:\n"})
    malformed_function = function.model_copy(update={
        "source_bundle": malformed_source,
        "source_bundle_sha256": hashlib.sha256(malformed_source).hexdigest(),
        "geometry_content_digest": hashlib.sha256(malformed_source).hexdigest(),
    })
    malformed_bindings = (mapped, malformed_function)
    malformed_lifecycle = child._GeometryBindingLifecycle(malformed_bindings)
    monkeypatch.setattr(child, "read_geometry_payload", original_read)
    monkeypatch.setattr(child, "_compile_geometry", original_compile)
    mapped_request = request("mapped", malformed_bindings)
    mapped_result, _ = _evaluate_current_invocation(
        mapped_request, program,
        child._ProductRealizationContext(mapped_request, prepared=malformed_lifecycle),
        output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
    )
    assert mapped_result.instance_receipts[0].binding_key == "body_geometry"

    malformed_request = request("function", malformed_bindings)
    with pytest.raises(PythonBundleValidationError):
        _evaluate_current_invocation(
            malformed_request, program,
            child._ProductRealizationContext(malformed_request, prepared=malformed_lifecycle),
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
        )


@pytest.mark.asyncio
async def test_mixed_numeric_runtime_covers_three_needs_and_batch_isolation(
    tmp_path: Path, caplog,
) -> None:
    import logging
    from CAD2BIM_backend.infrastructure.product_realization_runtime.protocol import (
        _geometry_source_request_key,
    )

    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    first = _mixed_numeric_request(tmp_path)
    second = _mixed_numeric_request(tmp_path, product_origin=(20.0, 20.0, 30.0))
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        local_single = await runtime.realize(first)
        preview_single = await runtime.realize_geometry(first, include_instances=True)
        solved_batch = [
            value
            async for value in runtime.realize_batch(
                (first, second), output_need=ProductRealizationOutputNeed.SOLVE_ONLY)
        ]
        local_prechecks = [value async for value in runtime.precheck_batch((first, second))]
        assert len(local_prechecks) == 2
        local_batch = [value async for value in runtime.instantiate_batch(
            (first, second), local_prechecks,
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)]
    finally:
        await runtime.close()

    assert [item.fragment_key for item in local_single.instance_receipts] == [
        "mapped_primary", "mapped_secondary", "function_shape"]
    assert local_single.instance_receipts[0].uniform_scale == 2.0
    assert local_single.instance_receipts[2].normalized_parameters == {
        "length_m": 0.4, "width_m": 0.2}
    assert local_single.frame_check_status == "PASS"
    assert all(item.evaluation.satisfied for item in local_single.frame_diagnostics)
    assert local_single.external_condition_checks[0].evaluation.satisfied

    assert preview_single.output_need is ProductRealizationOutputNeed.PREVIEW_WORLD
    assert preview_single.geometry is not None
    assert preview_single.geometry.summary.topology.solid_count == 3
    assert set(preview_single.instance_geometries) == {
        "mapped_primary", "mapped_secondary", "function_shape"}

    assert len(solved_batch) == 2
    assert all(item.geometry is None and item.instance_geometries is None for item in solved_batch)
    assert all(item.realization.instance_receipts == () for item in solved_batch)
    assert len(local_batch) == 2
    assert all(item.geometry is None for item in local_batch)
    assert all(set(item.instance_geometries) == {
        "mapped_primary", "mapped_secondary", "function_shape"} for item in local_batch)
    assert local_batch[0].realization is not local_batch[1].realization
    for item, precheck in zip(local_batch, local_prechecks, strict=True):
        assert len(item.realization.instance_receipts) == 3
        assert [receipt.model_dump(exclude={"geometry_sha256", "geometry_summary"})
                for receipt in item.realization.instance_receipts] == [
                    evaluation.model_dump() for evaluation in precheck.interface_evaluations]
    first_receipt, second_receipt = (item.realization.instance_receipts[2]
                                     for item in local_batch)
    assert first_receipt is not second_receipt
    assert local_batch[0].instance_geometries["function_shape"].payload == (
        local_batch[1].instance_geometries["function_shape"].payload)
    function_keys = [
        _geometry_source_request_key(
            next(binding for binding in request.geometry_bindings
                 if binding.binding_key == receipt.binding_key), receipt, exact=True)
        for request, receipt in zip((first, second), (first_receipt, second_receipt), strict=True)
    ]
    assert function_keys[0] == function_keys[1]
    assert [
        item.realization.component_project_world_transforms[1]
        .project_world_transform.translation_m[0]
        for item in local_batch
    ] == [12.0, 22.0]

    child_event_groups = [
        record.cad2bim_execution_phase["details"].get("child_timing", [])
        for record in caplog.records
        if hasattr(record, "cad2bim_execution_phase")
        and record.cad2bim_execution_phase["component"] == "worker_runtime"
        and record.cad2bim_execution_phase["phase"] == "process"
        and record.cad2bim_execution_phase["transition"] == "end"
    ]
    local_batch_events = next(events for events in child_event_groups
        if len([event for event in events if event["phase"] == "batch_item"]) == 2
        and any(event["phase"] == "reopen_mapped_template" for event in events))
    numeric_groups = [events for events in child_event_groups
        if any(event["phase"] == "family_numeric_entry" for event in events)]
    assert sum(len([event for event in events if event["phase"] == "family_numeric_entry"])
               for events in numeric_groups) == 6
    for events in numeric_groups:
        numeric = [event for event in events if event["phase"] == "family_numeric_entry"]
        outer = [event for event in events if event["phase"] == "author_entry"]
        assert len(numeric) == len(outer)
        for item, entry in zip(outer, numeric, strict=True):
            assert item["start_ns"] <= entry["start_ns"] < entry["end_ns"] <= item["end_ns"]
    batch_items = [event for event in local_batch_events if event["phase"] == "batch_item"]
    assert [event["details"]["item_index"] for event in batch_items] == [0, 1]
    assert len([event for event in local_batch_events
                if event["phase"] == "reopen_mapped_template"]) == 1
    assert len([event for event in local_batch_events
                if event["phase"] == "compile_geometry"]) == 1
    builds = [event for event in local_batch_events if event["phase"] == "geometry_build"]
    assert len(builds) == 1
    assert (batch_items[0]["start_ns"] <= builds[0]["start_ns"]
            < builds[0]["end_ns"] <= batch_items[0]["end_ns"])
    # Host reuses the checked local source while retaining each item's own receipts and pose.
    reused_bindings = [
        [event["details"]["binding_key"] for event in local_batch_events
         if event["phase"] == "geometry_reused_source_receipt"
         and item["start_ns"] <= event["start_ns"] < event["end_ns"] <= item["end_ns"]]
        for item in batch_items
    ]
    assert reused_bindings == [[], ["mapped_geometry", "mapped_geometry", "function_geometry"]]


def test_numeric_solve_only_validates_complete_proposal_before_zero_geometry(
    tmp_path: Path, monkeypatch,
) -> None:
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    request = _request(_mapped_binding(tmp_path))
    program = child._compile_family(
        request.source_bundle, "test_numeric_solve_only", "realize",
    )

    class ForbiddenPreparation:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("SOLVE_ONLY reached Geometry preparation")

    monkeypatch.setattr(child, "_PreparedGeometryBindings", ForbiddenPreparation)
    result, preview = _evaluate_current_invocation(
        request,
        program,
        child._ProductRealizationContext(request),
        output_need=ProductRealizationOutputNeed.SOLVE_ONLY,
    )

    assert result.proposal.status is ProductRealizationStatus.SOLVED
    assert result.instance_receipts == ()
    assert result.component_project_world_transforms == ()
    assert result.interface_project_world_frames == ()
    assert result.frame_check_status is None
    assert preview is None


def test_numeric_local_fragments_build_only_after_complete_preflight(
    tmp_path: Path, monkeypatch,
) -> None:
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    request = _request(_mapped_binding(tmp_path))
    program = child._compile_family(request.source_bundle, "test_numeric_local", "realize")
    calls = []
    original = child._PreparedGeometryBindings

    class ObservedPreparation(original):
        def __init__(self, bindings, *, parameter_rules=None):
            calls.append(("prepare", tuple(item.binding_key for item in bindings)))
            super().__init__(bindings, parameter_rules=parameter_rules)

        def load(self, indexed_bindings):
            calls.append(("load", tuple(binding.binding_key for _, binding in indexed_bindings)))
            return super().load(indexed_bindings)

    monkeypatch.setattr(child, "_PreparedGeometryBindings", ObservedPreparation)
    result, preview = _evaluate_current_invocation(
        request,
        program,
        child._ProductRealizationContext(request, include_instances=True),
        output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
    )

    assert calls == [("prepare", ()), ("load", ()), ("load", ("body_geometry",))]
    assert [item.fragment_key for item in result.instance_receipts] == ["body_shape"]
    assert preview is None


@pytest.mark.parametrize(
    "output_need",
    [ProductRealizationOutputNeed.LOCAL_FRAGMENTS, ProductRealizationOutputNeed.PREVIEW_WORLD],
)
def test_invalid_complete_numeric_solution_stops_before_geometry(
    tmp_path: Path, monkeypatch, output_need,
) -> None:
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    source = pack_source_bundle({"main.py": b"""def realize(ctx):
    return {'status':'SOLVED', 'components':[{'component_key':'body'}],
            'fragments':[{'fragment_key':'unknown_shape','binding_key':'body_geometry'}]}
"""})
    request = _request(_mapped_binding(tmp_path), source=source)
    program = child._compile_family(request.source_bundle, "test_invalid_numeric", "realize")

    class ForbiddenPreparation:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("invalid numeric proposal reached Geometry preparation")

    monkeypatch.setattr(child, "_PreparedGeometryBindings", ForbiddenPreparation)
    rejected, payload = _evaluate_current_invocation(
        request,
        program,
        child._ProductRealizationContext(request),
        output_need=output_need,
    )
    assert rejected.proposal.status is ProductRealizationStatus.CONTRACT_REJECTED
    assert payload is None


def test_numeric_batch_lifecycle_reuses_template_and_isolates_each_item(
    tmp_path: Path, monkeypatch,
) -> None:
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    binding = _mapped_binding(tmp_path)
    requests = tuple(
        _request(
            binding,
            invocation=ProductRealizationInvocation(
                product_origin=(origin, 0.0, 0.0), length_unit=IfcLengthUnit(),
            ),
        )
        for origin in (0.0, 2.0, 4.0)
    )
    lifecycle = child._GeometryBindingLifecycle((binding,))
    program = child._compile_family(requests[0].source_bundle, "test_numeric_batch", "realize")
    reopens = []
    child._load_geometry_dependencies()
    original = child.read_geometry_payload
    monkeypatch.setattr(
        child,
        "read_geometry_payload",
        lambda geometry: (reopens.append(geometry.sha256), original(geometry))[1],
    )
    results, contexts = [], []
    for request in requests:
        context = child._ProductRealizationContext(request, prepared=lifecycle)
        result, preview = _evaluate_current_invocation(
            request, program, context,
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
        )
        assert preview is None
        contexts.append(context)
        results.append(result)

    assert reopens == [binding.geometry.sha256]
    assert [result.component_project_world_transforms[0].project_world_transform.translation_m[0]
            for result in results] == [0.0, 2.0, 4.0]
    assert len({id(context._instance_shapes) for context in contexts}) == 3
    assert len({id(context._instance_shapes["body_shape"]) for context in contexts}) == 3


@pytest.mark.parametrize("defer", [False, True])
def test_product_local_serialization_preserves_exact_validation_owner(tmp_path, monkeypatch, defer):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child
    from CAD2BIM_backend.infrastructure.geometry_runtime import codec
    request = _scaling_batch_requests(tmp_path)[0]
    child._load_geometry_dependencies()
    original = child.save_user_shape
    reopened = child.reopen_user_shape
    child_reopens = []
    def child_open(payload):
        child_reopens.append(payload)
        return reopened(payload)
    monkeypatch.setattr(child, "reopen_user_shape", child_open)
    validations = []
    def saved(shape):
        validations.append("product")
        return original(shape)
    monkeypatch.setattr(child, "save_user_shape", saved)
    context = child._ProductRealizationContext(request, include_instances=defer,
        include_summary=False, defer_validation_to_codec=defer)
    program = child._compile_family(request.source_bundle, "test_serialization_owner", "realize")
    result, preview = _evaluate_current_invocation(
        request, program, context, output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)
    assert preview is None
    assert context.receipts[0].geometry_summary is None
    assert validations == ([] if defer else ["product"])
    # The selected named input still opens once; local output adds no reopen.
    assert child_reopens == [request.geometry_bindings[0].geometry_inputs[0].payload]
    if defer:
        validate = codec._validate_brep
        def checked(shape):
            validations.append("codec")
            return validate(shape)
        monkeypatch.setattr(codec, "_validate_brep", checked)
        geometry = codec.normalize_geometry(GeometryNormalizeRequest(format="occt_brep_v1",
            payload=context._instance_payloads["body_shape"], include_summary=False))
        assert geometry.sha256 == context.receipts[0].geometry_sha256
        assert geometry.summary is None
        assert validations == ["codec"]


def test_deferred_receipt_and_world_use_saved_geometry_bytes(tmp_path, monkeypatch):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    request = _scaling_batch_requests(tmp_path)[0]
    child._load_geometry_dependencies()
    original = child.serialize_user_shape
    def shifted_bytes(shape):
        return original(shape.translate(cq.Vector(0, 0.125, 0)))
    monkeypatch.setattr(child, "serialize_user_shape", shifted_bytes)
    context = child._ProductRealizationContext(request, include_instances=True,
        include_summary=True, defer_validation_to_codec=True)
    program = child._compile_family(request.source_bundle, "test_saved_geometry_bytes", "realize")
    result, preview = _evaluate_current_invocation(
        request, program, context, output_need=ProductRealizationOutputNeed.PREVIEW_WORLD)

    receipt = result.instance_receipts[0]
    local = normalize_geometry(GeometryNormalizeRequest(format="occt_brep_v1",
        payload=context._instance_payloads[receipt.fragment_key]))
    assert receipt.geometry_sha256 == local.sha256
    assert receipt.geometry_summary == local.summary
    assert preview is not None
    world = normalize_geometry(GeometryNormalizeRequest(format="occt_brep_v1", payload=preview[0]))
    assert world.summary.bounds.minimum_m[1] > local.summary.bounds.minimum_m[1]


@pytest.mark.parametrize("preview", [False, True])
def test_deferred_corrupt_saved_bytes_fail_at_actual_validation_owner(tmp_path, monkeypatch, preview):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    request = _scaling_batch_requests(tmp_path)[0]
    child._load_geometry_dependencies()
    monkeypatch.setattr(child, "serialize_user_shape", lambda _shape: b"corrupt BREP")
    context = child._ProductRealizationContext(request, include_instances=True,
        include_summary=preview, defer_validation_to_codec=True)
    program = child._compile_family(request.source_bundle, "test_corrupt_saved_bytes", "realize")
    if preview:
        with pytest.raises(child._ChildError) as failure:
            _evaluate_current_invocation(request, program, context,
                output_need=ProductRealizationOutputNeed.PREVIEW_WORLD)
        assert failure.value.code == "REALIZATION_OUTPUT_INVALID"
        assert context.receipts == ()
        assert context._instance_payloads == {}
    else:
        from CAD2BIM_backend.domain.geometry_runtime import GeometryRuntimeValidationError
        result, world = _evaluate_current_invocation(request, program, context,
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)
        assert world is None
        payload = context._instance_payloads["body_shape"]
        assert payload == b"corrupt BREP"
        assert result.instance_receipts[0].geometry_sha256 == hashlib.sha256(payload).hexdigest()
        with pytest.raises(GeometryRuntimeValidationError, match="could not be reopened"):
            normalize_geometry(GeometryNormalizeRequest(format="occt_brep_v1", payload=payload, include_summary=False))


def test_local_function_cache_bounds_and_exact_parameter_identity(monkeypatch):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child
    lifecycle = child._GeometryBindingLifecycle(())
    binding, first_pose, second_pose = _rigid_two_port_reuse_inputs()
    def key(binding_key, parameters, scale):
        selected = binding.model_copy(update={"binding_key": binding_key})
        return lifecycle.local_result_key(selected, SimpleNamespace(
            normalized_parameters=parameters, uniform_scale=scale))
    keys = [
        key("body", {"first": 1, "second": 2}, 1.0),
        key("body", {"second": 2, "first": 1}, 1.0),
        key("body", {"first": 1.0, "second": 2}, 1.0),
        key("body", {"first": True, "second": 2}, 1.0),
        key("body", {"first": 1, "second": 2}, 2.0),
        key("body", {"first": -0.0}, 1.0),
        key("body", {"first": 0.0}, 1.0),
    ]
    assert len(set(keys)) == len(keys)
    assert key("other_alias", {"first": 1, "second": 2}, 1.0) == keys[0]
    changed_source = binding.model_copy(update={"geometry_content_digest": "a" * 64})
    assert lifecycle.local_result_key(changed_source, SimpleNamespace(
        normalized_parameters={"first": 1, "second": 2}, uniform_scale=1.0)) != keys[0]
    assert key("body", first_pose, 1.0) != key("other_alias", second_pose, 1.0)
    monkeypatch.setattr(child, "_LOCAL_RESULT_CACHE_BYTES", 16)
    monkeypatch.setattr(child, "_LOCAL_RESULT_CACHE_ITEMS", 2)
    lifecycle.remember_local_result("a", b"first", None)
    lifecycle.remember_local_result("b", b"second", None)
    assert lifecycle.local_result("a")[0] == b"first"
    lifecycle.remember_local_result("c", b"third", None)
    assert lifecycle.local_result("b") is None
    assert lifecycle.local_result("c")[0] == b"third"
    lifecycle.remember_local_result("large", b"x" * 17, None)
    assert lifecycle.local_result("large") is None
    assert lifecycle.local_result("a")[0] == b"first"
    assert lifecycle._local_result_bytes <= 16
    assert child._GeometryBindingLifecycle(()).local_result("a") is None


def test_local_row_reuses_existing_bytes_after_batch_cache_eviction(monkeypatch):
    from CAD2BIM_backend.domain.ifc_geometry_construction import GeometryConstruction, GeometryConstructionNode
    from CAD2BIM_backend.domain.product_realization import ProductGeometryInterfaceEvaluation
    from CAD2BIM_backend.infrastructure.geometry_runtime import modeling_execution
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    source = pack_source_bundle({"main.py": b"def build(ctx):\n    return None\n"})
    binding = ResolvedModelingFunctionBinding(binding_key="body_geometry",
        geometry_content_digest=hashlib.sha256(source).hexdigest(), source_bundle=source,
        source_bundle_sha256=hashlib.sha256(source).hexdigest(),
        parameter_schema={"type": "object", "properties": {"value": {"type": "integer"}}},
        ifc_representation_route=IfcGeometryRepresentationRoute.PARAMETRIC)
    request = _request(binding, parameter_sources={"value": {"source_kind": "fixed", "value": 0}})
    context = child._ProductRealizationContext(request, include_instances=True,
        capture_construction=True, include_summary=False, defer_validation_to_codec=True)
    context._bindings["same_source_alias"] = binding.model_copy(update={"binding_key": "same_source_alias"})
    program = SimpleNamespace(sources={"main.py": "def build(ctx): return None"})
    context._function_programs = {key: program for key in context._bindings}
    context._function_geometries = {key: {} for key in context._bindings}
    builds = []
    def build(_program, parameters, _inputs, **options):
        builds.append(parameters["value"])
        return (f"owned-brep-{parameters['value']}".encode(), (),
                GeometryConstruction(root=0, nodes=(GeometryConstructionNode(id=0, op="test",
                    kwargs={"value": parameters["value"]}),)))
    monkeypatch.setattr(modeling_execution, "execute_geometry_build", build)
    monkeypatch.setattr(child, "serialize_user_shape", lambda value: value, raising=False)
    monkeypatch.setattr(child, "_LOCAL_RESULT_CACHE_ITEMS", 128)
    def add(index, value, key="body_geometry", target=context):
        fragment = SimpleNamespace(fragment_key=f"fragment_{index}", binding_key=key,
            geometry_parameters={"value": value}, uniform_scale=1.)
        evaluation = ProductGeometryInterfaceEvaluation(fragment_key=fragment.fragment_key,
            binding_key=key, normalized_parameters=fragment.geometry_parameters)
        target._build_fragment(fragment, evaluation)
        return evaluation

    first = add(0, 0)
    for index in range(1, 129):
        add(index, index)
    assert context._prepared.local_result(context._prepared.local_result_key(binding, first)) is None
    add(129, 0, "same_source_alias")
    assert builds == list(range(129))
    assert context._instance_payloads["fragment_129"] is context._instance_payloads["fragment_0"]
    assert len(context.receipts) == 130 and context.receipts[-1].binding_key == "same_source_alias"
    assert context.receipts[-1].geometry_sha256 == context.receipts[0].geometry_sha256
    assert "fragment_129" not in context._instance_shapes
    assert context._instance_payload_bytes == sum(map(len, context._instance_payloads.values()))
    context._instance_constructions["fragment_0"].nodes[0].kwargs["changed"] = True
    assert "changed" not in context._instance_constructions["fragment_129"].nodes[0].kwargs
    monkeypatch.setattr(child, "MAX_PRODUCT_REALIZATION_INSTANCE_GEOMETRY_BYTES",
        context._instance_payload_bytes + len(context._instance_payloads["fragment_0"]) - 1)
    with pytest.raises(ValueError, match="aggregate byte budget"):
        add(130, 0)
    assert builds == list(range(129))

    # A preview context keeps its existing build/summary path, even when its
    # parameters match. This unit probe never creates a native shape/Worker.
    preview = child._ProductRealizationContext(request, include_instances=True,
        capture_construction=True, include_summary=True, defer_validation_to_codec=True)
    preview._function_programs = {"body_geometry": program}
    preview._function_geometries = {"body_geometry": {}}
    monkeypatch.setattr(child, "reopen_user_shape", lambda value: value, raising=False)
    monkeypatch.setattr(child, "summarize_shape", lambda _value: None, raising=False)
    add(0, 0, target=preview)
    add(1, 0, target=preview)
    assert builds[-2:] == [0, 0] and len(builds) == 131
    assert preview._local_source_fragments == {}


def _rigid_two_port_reuse_inputs():
    from copy import deepcopy
    source_file = Path(__file__).resolve().parents[2] / "scenarios/engineering_product_families/geometry/curved_two_port/validation_samples.json"
    source = json.loads(source_file.read_text())[1]
    body = pack_source_bundle({"main.py": b"def build(ctx):\n    return None\n"})
    binding = ResolvedModelingFunctionBinding(binding_key="arbitrary_binding_name",
        geometry_content_digest=hashlib.sha256(body).hexdigest(), source_bundle=body,
        source_bundle_sha256=hashlib.sha256(body).hexdigest(), parameter_schema={},
        source_reuse_mode="rigid_two_port")
    # The representative may itself be away from the fragment origin.
    source["shape"]["local_center_m"] = [.5, -.2, .3]
    for port in source["ports"].values():
        port["origin_m"] = [value + delta for value, delta in
                            zip(port["origin_m"], source["shape"]["local_center_m"], strict=True)]
    target = deepcopy(source)
    rotate = lambda v: [v[2], v[0], v[1]]
    translation = [3., -2., 1.]
    place = lambda v: [value + delta for value, delta in zip(rotate(v), translation, strict=True)]
    target["shape"]["local_center_m"] = place(source["shape"]["local_center_m"])
    target["shape"]["frame_x"] = rotate(source["shape"]["frame_x"])
    for key, port in target["ports"].items():
        port["origin_m"] = place(source["ports"][key]["origin_m"])
        port["axis"] = rotate(source["ports"][key]["axis"])
        # Current numeric evaluation owns reference roll; it is not body pose.
        axis = port["axis"]
        preferred = [0., 0., 1.] if abs(axis[2]) < .9 else [1., 0., 0.]
        dot = sum(a * b for a, b in zip(axis, preferred, strict=True))
        projected = [a - dot * b for a, b in zip(preferred, axis, strict=True)]
        port["reference_direction"] = [v / math.hypot(*projected) for v in projected]
    return binding, source, target


@pytest.mark.parametrize("uniform_scale", [1., 2.5])
def test_known_two_port_source_reuse_retains_original_build_and_scaled_rigid_pose(uniform_scale):
    from CAD2BIM_backend.domain.product_realization import (
        geometry_source_request_parameters, geometry_source_to_fragment,
    )
    binding, source, target = _rigid_two_port_reuse_inputs()
    assert geometry_source_request_parameters(binding, source) == geometry_source_request_parameters(binding, target)
    actual = geometry_source_to_fragment(binding, source, target, uniform_scale=uniform_scale)
    assert actual.rotation == ((0., 0., 1.), (1., 0., 0.), (0., 1., 0.))
    assert actual.translation_m == pytest.approx(tuple(v * uniform_scale for v in (3., -2., 1.)), abs=1e-14)
    assert geometry_source_to_fragment(binding, source, source, uniform_scale=uniform_scale) == RigidTransform3D()


@pytest.mark.parametrize("mismatch", ["point", "axis", "radial", "scalar"])
def test_known_two_port_source_reuse_requires_actual_endpoint_axis_and_complete_scalar_match(mismatch):
    from CAD2BIM_backend.domain.product_realization import geometry_source_to_fragment
    binding, source, target = _rigid_two_port_reuse_inputs()
    if mismatch == "point":
        target["ports"]["side_a"]["origin_m"][0] += 1e-5
    elif mismatch == "axis":
        target["ports"]["side_b"]["axis"] = [0., 0., -1.]
    elif mismatch == "radial":
        target["shape"]["frame_x"] = [-v for v in target["shape"]["frame_x"]]
    else:
        target["shape"]["arc_radius_m"] = math.nextafter(target["shape"]["arc_radius_m"], math.inf)
    assert geometry_source_to_fragment(binding, source, target) is None


def test_other_geometry_and_unsupported_branch_keep_complete_source_parameters():
    from CAD2BIM_backend.domain.product_realization import (
        geometry_source_request_parameters, geometry_source_to_fragment,
    )
    binding, source, target = _rigid_two_port_reuse_inputs()
    author_binding = binding.model_copy(update={"source_reuse_mode": "exact"})
    assert geometry_source_request_parameters(author_binding, source) is source
    assert geometry_source_to_fragment(author_binding, source, target) is None
    assert geometry_source_to_fragment(author_binding, source, source) == RigidTransform3D()
    source["shape"]["mode"] = "straight"
    assert geometry_source_request_parameters(binding, source) is source
    assert geometry_source_to_fragment(binding, source, target) is None


def _scaling_batch_requests(tmp_path, *, fault=False):
    base = _mapped_binding(tmp_path)
    geometry_source = pack_source_bundle({
        "main.py": ("from .helpers import fresh\ndef build(ctx):\n"
            "    scale=ctx.param('scale')+fresh()-1\n    shape=ctx.geometry('body')\n"
            + ("    if scale==2:\n        return shape.missing_api()\n" if fault else "")
            + "    return shape.scale(scale)\n").encode(),
        "interfaces.py": b"def evaluate(ctx):\n    ctx.define_interface(key='datum',origin_m=(0,0,0),axis=(0,0,1),reference_direction=(1,0,0))\n",
        "helpers.py": b"def fresh(values=[]):\n    values.append(1)\n    return len(values)\n",
    })
    binding = ResolvedModelingFunctionBinding(binding_key="body_geometry", geometry_content_digest=hashlib.sha256(geometry_source).hexdigest(),
        source_bundle=geometry_source, source_bundle_sha256=hashlib.sha256(geometry_source).hexdigest(),
        parameter_schema={"type":"object","properties":{"scale":{"type":"number","minimum":0.5,"maximum":3}},
                          "required":["scale"],"additionalProperties":False},
        geometry_inputs=(GeometryBuildInput(input_key="body",payload=base.geometry.payload,sha256=base.geometry.sha256),))
    family = current_family_contract(public_parameters=[{"parameter_kind":"number","parameter_key":"scale","minimum":0.5,"maximum":3}],
        parameter_sources={"scale":{"source_kind":"public_parameter","parameter_key":"scale"}})
    source = pack_source_bundle({"main.py": b"""def realize(ctx):
    return {'status':'SOLVED','components':[{'component_key':'body'}],'fragments':[{'fragment_key':'body_shape','binding_key':'body_geometry'}]}
"""})
    return tuple(_family_request(family, binding, source, invocation=ProductRealizationInvocation(
                    public_parameters={"scale":scale}, product_origin=(0.0, 0.0, 0.0),
                    length_unit=IfcLengthUnit()))
                 for scale in (1,2,1))


def _model_interface_pair_request(
    tmp_path: Path,
    *,
    right_frame: dict[str, object],
    relation: dict[str, object],
    include_right_interface: bool = True,
    include_opposed_full_pair: bool = False,
) -> ProductRealizationRequest:
    base = _mapped_binding(tmp_path)
    interfaces: list[dict[str, object]] = [
        current_model_interface().model_dump(mode="python"),
        {
            "interface_key": "model_a",
            "local_frame": {},
            "profile": {"kind": "circular", "diameter_m": 0.2},
        },
    ]
    if include_right_interface:
        interfaces.append(
            {
                "interface_key": "model_b",
                "local_frame": right_frame,
                "profile": {"kind": "circular", "diameter_m": 0.2},
            }
        )
    family_interfaces: list[dict[str, object]] = [
        {
            "interface_key": "family_a",
            "owner_component_key": "body",
            "interface_kind": "internal",
            "source": {
                "source_kind": "model_interface",
                "fragment_key": "body_shape",
                "model_interface_key": "model_a",
            },
        },
        {
            "interface_key": "family_b",
            "owner_component_key": "body",
            "interface_kind": "internal",
            "source": {
                "source_kind": "model_interface",
                "fragment_key": "body_shape",
                "model_interface_key": "model_b",
            },
        },
    ]
    constraints: list[dict[str, object]] = [
        {
            "constraint_kind": "interface_coincidence",
            "constraint_key": "aligned_pair",
            "interface_a_key": "family_a",
            "interface_b_key": "family_b",
            "position_tolerance_m": 1e-6,
            "angular_tolerance_rad": 1e-6,
            "frame_relation": relation,
        }
    ]
    if include_opposed_full_pair:
        interfaces.extend(
            (
                {
                    "interface_key": "model_c",
                    "local_frame": {"translation_m": (0.3, 0.0, 0.0)},
                    "profile": {"kind": "circular", "diameter_m": 0.2},
                },
                {
                    "interface_key": "model_d",
                    "local_frame": {
                        "translation_m": (0.3, 0.0, 0.0),
                        "rotation": (
                            (1.0, 0.0, 0.0),
                            (0.0, -1.0, 0.0),
                            (0.0, 0.0, -1.0),
                        ),
                    },
                    "profile": {"kind": "circular", "diameter_m": 0.2},
                },
            )
        )
        for suffix in ("c", "d"):
            family_interfaces.append(
                {
                    "interface_key": f"family_{suffix}",
                    "owner_component_key": "body",
                    "interface_kind": "internal",
                    "source": {
                        "source_kind": "model_interface",
                        "fragment_key": "body_shape",
                        "model_interface_key": f"model_{suffix}",
                    },
                }
            )
        constraints.append(
            {
                "constraint_kind": "interface_coincidence",
                "constraint_key": "opposed_pair",
                "interface_a_key": "family_c",
                "interface_b_key": "family_d",
                "position_tolerance_m": 1e-6,
                "angular_tolerance_rad": 1e-6,
                "frame_relation": {
                    "direction_relation": "opposed",
                    "orientation_requirement": "full_frame",
                    "profile_requirement": "equal",
                },
            }
        )
    binding = ResolvedMappedGeometryBinding(
        binding_key=base.binding_key,
        geometry_content_digest=base.geometry_content_digest,
        geometry=base.geometry,
        interfaces=tuple(interfaces),
    )
    family = ProductFamilyContract.model_validate(
        {
            **_family().model_dump(mode="json", by_alias=True),
            "interfaces": family_interfaces,
            "constraints": constraints,
        }
    )
    source = pack_source_bundle(
        {
            "main.py": (
                b"""def realize(ctx):
    return {
        'status': 'SOLVED',
        'components': [{'component_key': 'body'}],
        'fragments': [{'fragment_key': 'body_shape', 'binding_key': 'body_geometry'}],
    }
"""
            )
        }
    )
    return _family_request(family, binding, source)


def test_rigid_transform_rejects_reflection() -> None:
    with pytest.raises(ValidationError, match="right-handed"):
        RigidTransform3D(rotation=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, -1.0)))


def test_axis_reference_frame_uses_xyz_columns_and_rejects_invalid_basis() -> None:
    frame = rigid_transform_from_axis_reference(
        (1.0, 2.0, 3.0),
        (0.0, 1.0, 0.0),
        (1.0, 0.0, 0.0),
    )

    assert frame.translation_m == (1.0, 2.0, 3.0)
    assert frame.rotation == (
        (1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0),
        (0.0, -1.0, 0.0),
    )
    with pytest.raises(ValueError, match="perpendicular"):
        rigid_transform_from_axis_reference(
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 1.0),
            (0.0, 0.0, 1.0),
        )


def test_authoring_expected_status_is_not_part_of_runtime_invocation() -> None:
    scenario = ProductRealizationValidationScenario(
        scenario_key="nominal",
        expected_status=ProductRealizationStatus.AMBIGUOUS,
        product_origin=(0.0, 0.0, 0.0),
        length_unit=IfcLengthUnit(),
    )

    invocation = scenario.to_invocation()

    assert invocation == ProductRealizationInvocation(
        product_origin=(0.0, 0.0, 0.0), length_unit=IfcLengthUnit())
    assert "scenario_key" not in invocation.model_dump()
    assert "expected_status" not in invocation.model_dump()


def test_materialization_preflights_total_scratch_before_writing_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(_mapped_binding(tmp_path))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(realization_protocol, "PRODUCT_REALIZATION_SCRATCH_BYTES", 1)

    with pytest.raises(ValueError, match="private scratch budget"):
        realization_protocol.materialize_request(request, scratch)

    assert tuple(scratch.iterdir()) == ()


def test_mapped_interfaces_round_trip_through_the_private_worker_protocol(
    tmp_path: Path,
) -> None:
    base = _mapped_binding(tmp_path)
    binding = ResolvedMappedGeometryBinding(
        binding_key=base.binding_key,
        geometry_content_digest=base.geometry_content_digest,
        geometry=base.geometry,
        interfaces=(
            {
                "interface_key": "outlet",
                "local_frame": {"translation_m": (0.5, 0.0, 0.0)},
                "profile": {"kind": "rectangle", "width_m": 0.2, "height_m": 0.1},
            },
        ),
    )
    request = _request(binding)
    scratch = tmp_path / "protocol"
    scratch.mkdir()

    control, source = realization_protocol.materialize_request(request, scratch)
    restored = realization_protocol.restore_request(control, source, scratch)

    assert restored.geometry_bindings[0] == binding


@pytest.mark.asyncio
async def test_named_brep_function_inputs_survive_protocol_and_realization_restart(tmp_path):
    base = _mapped_binding(tmp_path)
    source = pack_source_bundle(
        {'main.py': b"def build(ctx):\n    return ctx.geometry('body')\n",
'interfaces.py': b"def evaluate(ctx):\n    ctx.define_interface(key='datum', origin_m=(0, 0, 0), axis=(0, 0, 1), reference_direction=(1, 0, 0))\n"}
    )
    binding = ResolvedModelingFunctionBinding(
        binding_key="body_geometry",
        geometry_content_digest="a" * 64,
        source_bundle=source,
        source_bundle_sha256=hashlib.sha256(source).hexdigest(),
        parameter_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        geometry_inputs=(
            GeometryBuildInput(
                input_key="body", payload=base.geometry.payload, sha256=base.geometry.sha256
            ),
        ),
    )
    request = _request(binding)
    scratch = tmp_path / "input-protocol"
    scratch.mkdir()
    control, program = realization_protocol.materialize_request(request, scratch)
    restored = realization_protocol.restore_request(control, program, scratch)
    assert restored.geometry_bindings == request.geometry_bindings
    for _ in range(2):
        runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
        try:
            result = await runtime.realize(restored)
        finally:
            await runtime.close()
        receipt = result.instance_receipts[0]
        assert result.proposal.status is ProductRealizationStatus.SOLVED
        assert receipt.geometry_summary.topology.solid_count == 1
        assert receipt.geometry_summary.volume_m3 == pytest.approx(0.125)
    input_path = scratch / "input-000-00.brep"
    input_path.write_bytes(b"x" * len(base.geometry.payload))
    with pytest.raises(ValidationError, match="payload digest mismatch"):
        realization_protocol.restore_request(control, program, scratch)


def test_realization_source_cannot_reach_private_context_state() -> None:
    source = pack_source_bundle({"main.py": b"def realize(ctx):\n    return ctx._request\n"})

    with pytest.raises(PythonBundleValidationError, match="private runtime"):
        compile_python_bundle(
            source,
            package_name="cad2bim_realization_test",
            entry_name="realize",
            allowed_import_roots={"math", "cadquery", "OCP"},
            profile_label="Product realization",
            maximum_members=64,
            maximum_member_bytes=65_536,
            maximum_total_bytes=262_144,
            reject_private_attributes=True,
        )


def test_family_bundle_compiles_both_entries_once_and_loads_fresh_modules(monkeypatch):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child
    from CAD2BIM_backend.infrastructure.worker_runtime import python_user_code
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingArena

    source = pack_source_bundle({
        "main.py": b"from .helper import next_value\ndef realize(ctx):\n    return next_value()\ndef standard_model(ctx):\n    return next_value() + 10\n",
        "helper.py": b"values = []\ndef next_value():\n    values.append(1)\n    return len(values)\n",
    })
    parsed = []
    compiled = []
    original_parse = python_user_code.ast.parse
    original_compile = __import__("RestrictedPython", fromlist=["compile_restricted_exec"]).compile_restricted_exec

    def parse(*args, **kwargs):
        parsed.append(kwargs.get("filename"))
        return original_parse(*args, **kwargs)

    def compile_module(*args, **kwargs):
        compiled.append(kwargs.get("filename"))
        return original_compile(*args, **kwargs)

    monkeypatch.setattr(python_user_code.ast, "parse", parse)
    monkeypatch.setattr("RestrictedPython.compile_restricted_exec", compile_module)
    program = child._compile_family(source, "test_entry_bundle", "realize",
                                    required_entry_names=("realize", "standard_model"))
    assert sorted(parsed) == sorted(compiled) == ["<cad2bim-bundle:helper.py>", "<cad2bim-bundle:main.py>"]
    values = []
    for name in ("realize", "standard_model", "realize"):
        arena = ModelingArena(profile="family_numeric")
        try:
            entry = program.load_entry(arena.import_module, execution_globals=arena.globals(), entry_name=name)
            values.append(entry(None))
        finally:
            arena.close()
    assert values == [1, 11, 1]
    assert len(compiled) == 2
    with pytest.raises(PythonBundleValidationError, match="not validated"):
        program.load_entry(None, entry_name="next_value")


@pytest.mark.parametrize("main", [
    "def realize(ctx):\n    return 1\n",
    "def realize(ctx):\n    return 1\ndef standard_model(ctx, other):\n    return 2\n",
    "def realize():\n    return 1\ndef standard_model(ctx):\n    return 2\n",
    "def realize(ctx):\n    return 1\ndef standard_model(ctx):\n    return ctx._request\n",
])
def test_family_bundle_checks_every_declared_entry(main):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child
    with pytest.raises(PythonBundleValidationError):
        child._compile_family(pack_source_bundle({"main.py": main.encode()}), "test_bad_entries", "realize",
                              required_entry_names=("realize", "standard_model"))


def test_shared_bundle_single_entry_defaults_and_host_call_restriction():
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingArena
    from CAD2BIM_backend.infrastructure.worker_runtime.python_user_code import RestrictedImportGuard

    data = compile_python_bundle(pack_source_bundle({"main.py": b"def transform(ctx):\n    return 7\n"}),
        package_name="test_data_default", entry_name="transform", allowed_import_roots=(),
        profile_label="Data", maximum_members=2, maximum_member_bytes=1024, maximum_total_bytes=2048)
    assert data.entry_names == ("transform",)
    assert data.load_entry(RestrictedImportGuard(allowed_import_roots=()))(None) == 7
    with pytest.raises(PythonBundleValidationError, match="Host is the only caller"):
        compile_python_bundle(pack_source_bundle({"main.py": b"def transform(ctx):\n    return transform(ctx)\n"}),
            package_name="test_data_invalid", entry_name="transform", allowed_import_roots=(),
            profile_label="Data", maximum_members=2, maximum_member_bytes=1024, maximum_total_bytes=2048)
    geometry = child._compile_geometry(pack_source_bundle({'main.py': b'def build(ctx):\n    return 9\n',
'interfaces.py': b'def evaluate(ctx):\n    pass\n'}),
                                        "test_geometry_default", "build")
    arena = ModelingArena(profile="geometry")
    try:
        assert geometry.entry_names == ("build",)
        assert geometry.load_entry(arena.import_module, execution_globals=arena.globals())(None) == 9
    finally:
        arena.close()


def test_batch_needs_normalize_and_result_frames_bind_each_item():
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import WorkerProtocolError
    need = ProductRealizationOutputNeed
    normalize = realization_protocol.normalize_batch_output_needs
    assert normalize(need.SOLVE_ONLY, 2) == (need.SOLVE_ONLY,) * 2
    assert normalize((need.PREVIEW_WORLD, need.LOCAL_FRAGMENTS), 2) == (need.PREVIEW_WORLD, need.LOCAL_FRAGMENTS)
    for invalid in ((), (need.SOLVE_ONLY,), (need.SOLVE_ONLY, "invalid"), None):
        with pytest.raises((TypeError, ValueError)):
            normalize(invalid, 2)
    response = {"status": "succeeded", "item_index": 0,
                "output_need": need.LOCAL_FRAGMENTS.value, "result": {}, "geometries": [],
                "constructions": []}
    expected = dict(item_index=0, output_need=need.LOCAL_FRAGMENTS)
    realization_protocol.validate_batch_result_frame(response, **expected)
    for changed in ({"item_index": 1}, {"item_index": False},
                    {"output_need": need.PREVIEW_WORLD.value}, {"extra": True}, {"status": "finished"}):
        with pytest.raises(WorkerProtocolError):
            realization_protocol.validate_batch_result_frame({**response, **changed}, **expected)
    with pytest.raises(WorkerProtocolError):
        realization_protocol.validate_batch_result_frame({key: value for key, value in response.items() if key != "output_need"}, **expected)
    with pytest.raises(WorkerProtocolError):
        realization_protocol.validate_batch_result_frame({**response, "constructions": [
            {"fragment_key": "body", "byte_length": 1, "sha256": "invalid"}]}, **expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("group_limit", ("items", "framed_bytes", "single_framed_bytes"))
async def test_batch_codec_groups_preserve_each_record_output_need(tmp_path, monkeypatch, group_limit):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child
    from CAD2BIM_backend.infrastructure.geometry_runtime import codec_execution_protocol
    from CAD2BIM_backend.infrastructure.product_realization_runtime.protocol import ProductRealizationPreviewGeometryMetadata

    binding = _mapped_binding(tmp_path)
    request = _request(binding)
    program = child._compile_family(request.source_bundle, "test_codec_routing", "realize")
    solve_result, _ = _evaluate_current_invocation(request, program, child._ProductRealizationContext(request),
                                               output_need=ProductRealizationOutputNeed.SOLVE_ONLY)
    context = child._ProductRealizationContext(request, include_instances=True, include_summary=True)
    result, preview = _evaluate_current_invocation(request, program, context,
                                                 output_need=ProductRealizationOutputNeed.PREVIEW_WORLD)
    receipt = result.instance_receipts[0]
    # Child world BREP intentionally has no measurements until Codec normalizes
    # it. Supply that real normalized value to this grouping-only codec double.
    world = normalize_geometry(GeometryNormalizeRequest(format=CanonicalGeometryFormat.OCCT_BREP_V1,
        source_format=GeometrySourceFormat.OCCT_BREP, payload=preview[0]))
    local = binding.geometry.model_copy(update={"payload": context._instance_payloads[receipt.fragment_key],
                                                "sha256": receipt.geometry_sha256, "summary": receipt.geometry_summary})
    geometries = {value.sha256: value for value in (world, local)}
    def staged(name, handle, geometry):
        path = tmp_path / name
        path.write_bytes(geometry.payload)
        metadata = ProductRealizationPreviewGeometryMetadata.from_brep(geometry.payload, geometry.summary)
        return handle, metadata, path
    calls = []
    class Codec:
        async def normalize_batch(self, requests):
            codec_execution_protocol.normalize_batch_control(requests, include_raw_brep_summary=False)
            calls.append(len(requests))
            return tuple((geometries[hashlib.sha256(request.payload).hexdigest()], None) for request in requests)
        async def normalize_many(self, requests):
            calls.append("single")
            return tuple((geometries[hashlib.sha256(request.payload).hexdigest()], None) for request in requests)
        async def close(self):
            pass
    runtime = OcpCadQueryProductRealizationRuntime(codec_executor=Codec(), capacity_root=tmp_path / "capacity")
    records = [(result, [staged("world.brep", None, world), staged("world-local.brep", receipt.fragment_key, local)], [], ProductRealizationOutputNeed.PREVIEW_WORLD),
               (result, [staged("local.brep", receipt.fragment_key, local)], [], ProductRealizationOutputNeed.LOCAL_FRAGMENTS),
               (solve_result, [], [], ProductRealizationOutputNeed.SOLVE_ONLY)]
    if group_limit == "items":
        monkeypatch.setattr(codec_execution_protocol, "MAX_CODEC_BATCH_TOTAL_ITEMS", 2)
    else:
        requests = tuple(GeometryNormalizeRequest(format=value.format,
            source_format=GeometrySourceFormat.OCCT_BREP, payload=value.payload,
            include_summary=value.summary is not None) for value in (world, local))
        selected = requests if group_limit == "framed_bytes" else (max(requests, key=lambda r: len(r.payload)),)
        _, framed = codec_execution_protocol.normalize_batch_control(selected, include_raw_brep_summary=False)
        byte_limit = len(framed) + sum(len(request.payload) for request in selected) - 1
        assert sum(len(request.payload) for request in selected) <= byte_limit
        monkeypatch.setattr(codec_execution_protocol, "MAX_CODEC_BATCH_INPUT_BYTES", byte_limit)
        # Exercise a pending small fragment followed by a single-frame fallback.
        if group_limit == "single_framed_bytes":
            records[0][1].sort(key=lambda entry: entry[1].byte_length)
    try:
        values = [value async for value in runtime._normalize_batch_records(records)]
    finally:
        await runtime.close()
    if group_limit == "items":
        assert calls == [2, 1]
    elif group_limit == "framed_bytes":
        assert calls == [1, 1, 1]
    else:
        assert "single" in calls
    assert [value.output_need for value in values] == [record[3] for record in records]
    assert values[0].geometry == world and set(values[0].instance_geometries) == {receipt.fragment_key}
    assert values[1].geometry is None and set(values[1].instance_geometries) == {receipt.fragment_key}
    assert values[2].geometry is None and values[2].instance_geometries is None
    assert all(not path.exists() for _, outputs, _, _ in records for _, _, path in outputs)


@pytest.mark.asyncio
async def test_batch_codec_failure_keeps_reason_and_unconfirmed_group(tmp_path):
    from CAD2BIM_backend.infrastructure.product_realization_runtime.protocol import (
        ProductRealizationPreviewGeometryMetadata,
    )

    path = tmp_path / "changed.brep"
    path.write_bytes(b"bad")
    metadata = ProductRealizationPreviewGeometryMetadata.from_brep(b"old", None)
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(ProductRealizationRuntimeValidationError) as failure:
            await runtime._normalize_record_group(
                [(None, [("body", metadata, path)], [], ProductRealizationOutputNeed.LOCAL_FRAGMENTS)],
                start_index=7,
            )
        assert failure.value.item_index is None
        assert failure.value.diagnostic == {
            "phase": "codec", "group_start_index": 7, "group_item_count": 1,
            "reason": "product batch staged payload changed",
        }
        assert isinstance(failure.value.__cause__, GeometryRuntimeValidationError)
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ("unavailable", "resource_limit"))
async def test_batch_codec_technical_failure_keeps_original_reason_and_group(tmp_path, failure_kind):
    from CAD2BIM_backend.infrastructure.product_realization_runtime.protocol import (
        ProductRealizationPreviewGeometryMetadata,
    )

    payload = b"staged"
    path = tmp_path / "staged.brep"
    path.write_bytes(payload)
    metadata = ProductRealizationPreviewGeometryMetadata.from_brep(payload, None)
    original = (GeometryRuntimeUnavailableError("codec disappeared", diagnostic={"source": "codec"})
                if failure_kind == "unavailable" else GeometryRuntimeResourceLimitError("codec budget"))

    class FailingCodec:
        async def normalize_batch(self, _requests):
            raise original

        async def close(self):
            pass

    runtime = OcpCadQueryProductRealizationRuntime(
        codec_executor=FailingCodec(), capacity_root=tmp_path / "capacity",
    )
    error_class = (ProductRealizationRuntimeUnavailableError if failure_kind == "unavailable"
                   else ProductRealizationRuntimeResourceLimitError)
    try:
        with pytest.raises(error_class) as failure:
            await runtime._normalize_record_group(
                [(None, [("body", metadata, path)], [], ProductRealizationOutputNeed.LOCAL_FRAGMENTS)],
                start_index=4,
            )
        assert failure.value.item_index is None
        assert failure.value.diagnostic["phase"] == "codec"
        assert failure.value.diagnostic["group_start_index"] == 4
        assert failure.value.diagnostic["group_item_count"] == 1
        assert failure.value.diagnostic["reason"] == str(original)
        if failure_kind == "unavailable":
            assert failure.value.diagnostic["codec_diagnostic"] == {"source": "codec"}
        assert failure.value.__cause__ is original
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_later_codec_group_failure_preserves_only_the_earlier_yield(tmp_path, monkeypatch):
    from CAD2BIM_backend.infrastructure.geometry_runtime import codec_execution_protocol

    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    records = [(None, [("body", SimpleNamespace(byte_length=1), None)], [],
                ProductRealizationOutputNeed.LOCAL_FRAGMENTS) for _ in range(2)]
    monkeypatch.setattr(codec_execution_protocol, "MAX_CODEC_BATCH_TOTAL_ITEMS", 1)

    async def normalize_group(_records, *, start_index):
        if start_index == 1:
            raise ProductRealizationRuntimeValidationError("REALIZATION_OUTPUT_INVALID", "second group failed",
                phase="output_validation", diagnostic={"phase": "codec", "group_start_index": 1,
                                                       "group_item_count": 1})
        return ["confirmed"]

    monkeypatch.setattr(runtime, "_normalize_record_group", normalize_group)
    yielded = []
    try:
        with pytest.raises(ProductRealizationRuntimeValidationError) as failure:
            async for value in runtime._normalize_batch_records(records):
                yielded.append(value)
        assert yielded == ["confirmed"]
        assert failure.value.diagnostic["group_start_index"] == 1
    finally:
        await runtime.close()


def test_request_requires_normalized_public_parameter_closure(tmp_path: Path) -> None:
    family = _family().model_copy(
        update={
            "public_parameters": (
                ProductFamilyNumberParameterContract(
                    parameter_key="diameter",
                    minimum=0.01,
                    maximum=1.0,
                ),
            )
        }
    )
    source = _program()

    with pytest.raises(ValidationError, match="product family parameters are missing required keys.*diameter"):
        ProductRealizationRequest(
            family_contract=family,
            family_contract_digest=sha256_digest(family),
            source_bundle=source,
            source_bundle_sha256=hashlib.sha256(source).hexdigest(),
            geometry_bindings=(_mapped_binding(tmp_path),),
            invocation=ProductRealizationInvocation(
                product_origin=(0.0, 0.0, 0.0), length_unit=IfcLengthUnit()),
        )


@pytest.mark.asyncio
async def test_realization_executes_multimodule_program_with_exact_mapped_brep(
    tmp_path: Path,
) -> None:
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    request = _request(_mapped_binding(tmp_path))

    result = await runtime.realize(request)

    assert result.proposal.status is ProductRealizationStatus.SOLVED
    assert result.source_bundle_sha256 == request.source_bundle_sha256
    assert len(result.instance_receipts) == 1
    assert result.instance_receipts[0].binding_key == "body_geometry"
    assert result.instance_receipts[0].geometry_summary == request.geometry_bindings[0].geometry.summary
    assert result.instance_receipts[0].geometry_sha256 == request.geometry_bindings[0].geometry.sha256


def test_batch_protocol_rejects_changed_closure_and_reordered_frames(tmp_path):
    from CAD2BIM_backend.domain.product_realization import ProductRealizationOutputNeed
    request = _request(_mapped_binding(tmp_path))
    folder = tmp_path / "batch"
    folder.mkdir()
    needs = (ProductRealizationOutputNeed.LOCAL_FRAGMENTS, ProductRealizationOutputNeed.SOLVE_ONLY)
    control, source = realization_protocol.materialize_batch((request, request), folder,
        output_needs=needs)
    restored, header = realization_protocol.restore_batch_header(control, source, folder)
    assert restored == request and header["item_count"] == 2 and "output_need" not in header
    for index, need in enumerate(needs):
        frame = realization_protocol.batch_invocation_frame(index, request, output_need=need)
        assert realization_protocol.restore_batch_invocation(restored, frame, index) == (request, need)
        with pytest.raises(ValueError):
            realization_protocol.restore_batch_invocation(restored, {**frame, "output_need": "unknown"}, index)
        with pytest.raises(ValueError, match="fields"):
            realization_protocol.restore_batch_invocation(restored, {**frame, "extra": True}, index)
    with pytest.raises(ValueError, match="order"):
        realization_protocol.restore_batch_invocation(restored, realization_protocol.batch_invocation_frame(1, request, output_need=needs[1]), 0)
    altered = request.model_copy(update={"source_bundle_sha256": "0" * 64})
    with pytest.raises(ValueError, match="same exact"):
        realization_protocol.materialize_batch((request, altered), folder, output_needs=needs)


def test_prepared_batch_closure_owns_nested_values_and_checks_every_invocation(tmp_path):
    from CAD2BIM_backend.domain.product_realization import _PreparedProductRealizationRequest

    request = _scaling_batch_requests(tmp_path)[0]
    prepared = _PreparedProductRealizationRequest(request)
    frame = realization_protocol.batch_invocation_frame(0, request, output_need=ProductRealizationOutputNeed.SOLVE_ONLY)
    request.geometry_bindings[0].parameter_schema["properties"]["scale"]["maximum"] = 99
    restored, _ = realization_protocol.restore_batch_invocation(prepared, frame, 0)
    assert restored.geometry_bindings[0].parameter_schema["properties"]["scale"]["maximum"] == 3
    restored.geometry_bindings[0].parameter_schema["properties"]["scale"]["maximum"] = 77
    assert realization_protocol.restore_batch_invocation(prepared, frame, 0)[0].geometry_bindings[0].parameter_schema["properties"]["scale"]["maximum"] == 3
    with pytest.raises(ValueError, match="order"):
        realization_protocol.restore_batch_invocation(prepared, frame, 1)
    for parameters in ({}, {"scale": 99}, {"scale": "1"}, {"scale": True}):
        with pytest.raises(ValueError):
            prepared.restore_invocation({"public_parameters": parameters})
    with pytest.raises(ValueError, match="source bundle"):
        ProductRealizationRequest.model_validate(
            {**request.model_dump(), "source_bundle_sha256": "0" * 64}
        )


def test_worker_reuses_rules_and_finalizer_facts_but_host_checks_receipts(tmp_path, monkeypatch):
    from CAD2BIM_backend.domain import product_realization as domain
    from CAD2BIM_backend.domain import geometry_runtime as geometry_domain
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    requests = _scaling_batch_requests(tmp_path)
    moved = requests[1].invocation.model_copy(update={"product_origin": (2.0, 3.0, 4.0)})
    requests = (requests[0], requests[1].model_copy(update={"invocation": moved}), requests[2])
    prepare_calls, resolve_calls = [], []
    original_prepare = geometry_domain._safe_parameter_schema_validator
    original_resolve = domain.resolve_product_realization_world_facts
    monkeypatch.setattr(geometry_domain, "_safe_parameter_schema_validator",
                        lambda schema: (prepare_calls.append(1), original_prepare(schema))[1])
    monkeypatch.setattr(domain, "resolve_product_realization_world_facts",
                        lambda *args, **kwargs: (resolve_calls.append(1), original_resolve(*args, **kwargs))[1])
    prepared = child._GeometryBindingLifecycle(requests[0].geometry_bindings)
    program = child._compile_family(requests[0].source_bundle, "test_prepared_family", "realize")
    results = []
    for index, request in enumerate(requests):
        context = child._ProductRealizationContext(request, prepared=prepared,
            include_summary=False, defer_validation_to_codec=True)
        result, preview = _evaluate_current_invocation(
            request, program, context, output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)
        assert result.proposal.status is ProductRealizationStatus.SOLVED
        assert preview is None
        results.append(result)
    assert len(prepare_calls) == 1
    assert len(resolve_calls) == 3
    assert results[0] == results[2]
    assert results[0].instance_receipts[0].geometry_sha256 != results[1].instance_receipts[0].geometry_sha256
    for request, result in zip(requests, results):
        assert result == domain.finalize_product_realization(
            request, result.proposal, result.instance_receipts,
            interface_evaluations=result.interface_evaluations,
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)
    assert len(resolve_calls) == 6
    receipt = results[0].instance_receipts[0].model_copy(update={"normalized_parameters": {"scale": 2}})
    with pytest.raises(ValueError, match="receipt parameters differ"):
        domain.finalize_product_realization(
            requests[0], results[0].proposal, (receipt,),
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)


def test_instantiation_result_preserves_checked_facts_and_checks_new_receipts(tmp_path, monkeypatch):
    from CAD2BIM_backend.domain import product_realization as domain
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    request = _model_interface_pair_request(tmp_path, right_frame={}, relation={
        "direction_relation": "same", "orientation_requirement": "full_frame",
        "profile_requirement": "equal"})
    context = child._ProductRealizationContext(request, include_summary=False,
                                               defer_validation_to_codec=True)
    program = child._compile_family(request.source_bundle, "test_instantiation_delta", "realize")
    result, _ = _evaluate_current_invocation(request, program, context,
        output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)
    precheck = result.model_copy(update={"execution_stage": "interface_constraints_evaluated", "instance_receipts": ()})
    domain.validate_product_realization_result_integrity(request, result)
    # Numeric normalization already ran. The host must check Geometry's new
    # facts without invoking that complete normalization a second time.
    monkeypatch.setattr(domain, "validate_product_realization_proposal",
                        lambda *_a, **_kw: pytest.fail("repeated numeric normalization"))
    domain.validate_product_realization_instantiation_precheck(request, precheck)
    fragment = precheck.proposal.fragments[0]
    for update in (
        {"invocation": request.invocation.model_copy(update={"product_origin": (1., 0., 0.)})},
        {"proposal": precheck.proposal.model_copy(update={"components": ()})},
        {"proposal": precheck.proposal.model_copy(update={"fragments": ()})},
        {"proposal": precheck.proposal.model_copy(update={"fragments": (
            fragment.model_copy(update={"binding_key": "other_geometry"}),)})},
        {"interface_evaluations": ()},
        {"frame_check_status": "FAIL"},
    ):
        with pytest.raises(ValueError):
            domain.validate_product_realization_instantiation_precheck(request, precheck.model_copy(update=update))
    # Instantiation prepares only native assets, not another jsonschema import
    # or validator. The same lazy lifecycle remains available to Numeric.
    monkeypatch.setattr(child, "_prepare_geometry_binding_parameters",
                        lambda *_a: pytest.fail("instantiation prepared numeric schema rules"))
    lifecycle = child._GeometryBindingLifecycle(request.geometry_bindings)
    child._ProductRealizationContext(request, prepared=lifecycle)
    lifecycle.prepare(set())
    bindings = {binding.binding_key: binding for binding in request.geometry_bindings}
    reused = {receipt.fragment_key: realization_protocol.ProductRealizationPreviewGeometryMetadata.from_brep(
        bindings[receipt.binding_key].geometry.payload, receipt.geometry_summary,
    ) for receipt in result.instance_receipts}
    reused_context = child._ProductRealizationContext(request, prepared=lifecycle,
        include_instances=True, capture_construction=True, include_summary=False,
        defer_validation_to_codec=True, reused_fragments=reused)
    reused_result, reused_preview = child._instantiate_invocation(request, precheck, reused_context,
        output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)
    assert reused_result == result
    assert reused_preview is None
    assert reused_context._instance_shapes == reused_context._instance_payloads == {}
    assert reused_context._instance_constructions == {}
    assert lifecycle._loaded_keys == set()
    with pytest.raises(ValueError, match="local-fragment"):
        child._ProductRealizationContext(request, reused_fragments=reused)
    reused_context._reused_fragments["unknown_fragment"] = next(iter(reused.values()))
    with pytest.raises(child._ChildError, match="unknown fragment"):
        child._instantiate_invocation(request, precheck, reused_context,
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)
    monkeypatch.setattr(domain, "_validate_product_realization_result_facts",
                        lambda *_a, **_kw: pytest.fail("rechecked inherited numeric facts"))
    def check(value, frozen=precheck):
        domain.validate_product_realization_instantiation_result(request, frozen, value,
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)
    check(result)
    changed_component = result.proposal.components[0].model_copy(update={
        "local_transform": RigidTransform3D(translation_m=(1., 0., 0.))})
    changed_evaluation = result.interface_evaluations[0].model_copy(update={
        "interfaces": result.interface_evaluations[0].interfaces[::-1]})
    for update in (
        {"proposal": result.proposal.model_copy(update={"components": (changed_component,)})},
        {"interface_evaluations": (changed_evaluation,)},
        {"interface_project_world_frames": result.interface_project_world_frames[::-1]},
        {"binding_digest": "0" * 64},
        {"execution_stage": "interface_constraints_evaluated"},
    ):
        with pytest.raises(ValueError):
            check(result.model_copy(update=update))
    receipt = result.instance_receipts[0]
    for update in (
        {"fragment_key": "other_fragment"}, {"binding_key": "other_geometry"},
        {"normalized_parameters": {"undeclared": 1}}, {"uniform_scale": 2.0},
        {"interfaces": receipt.interfaces[::-1]},
    ):
        with pytest.raises(ValueError):
            check(result.model_copy(update={"instance_receipts": (receipt.model_copy(update=update),)}))
    with pytest.raises(ValueError):
        check(result, precheck.model_copy(update={"frame_check_status": "FAIL"}))


def test_worker_finalized_standard_model_uses_product_origin_and_rejects_v2_root_field(tmp_path):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    family = ProductFamilyContract.model_validate({
        **_family().model_dump(), "standard_model": {},
    })
    binding = _mapped_binding(tmp_path).model_copy(update={
        "interfaces": (current_model_interface(frame=RigidTransform3D(translation_m=(0.25, 0, 0))),),
    })
    request = _family_request(family, binding, _program(entry="standard_model"),
        invocation=ProductRealizationInvocation(realization_kind="standard_model",
            product_origin=(10.0, 20.0, 30.0), length_unit=IfcLengthUnit()))
    program = child._compile_family(request.source_bundle, "test_standard_facts", "standard_model")
    context = child._ProductRealizationContext(request, include_summary=False)
    result, preview = _evaluate_current_invocation(
        request, program, context, output_need=ProductRealizationOutputNeed.PREVIEW_WORLD)
    assert result.component_project_world_transforms[0].project_world_transform.translation_m == (10, 20, 30)
    assert child.reopen_user_shape(preview[0]).Center().toTuple() == pytest.approx((10, 20, 30))
    moved_source = pack_source_bundle({"main.py": b"""def standard_model(ctx):
    return {'status':'SOLVED','root_world_transform':{'translation_m':[1,0,0]},'components':[{'component_key':'body'}],'fragments':[{'fragment_key':'body_shape','binding_key':'body_geometry'}]}
"""})
    moved_program = child._compile_family(moved_source, "test_invalid_standard_root", "standard_model")
    rejected, payload = _evaluate_current_invocation(request, moved_program,
        child._ProductRealizationContext(request, include_summary=False),
        output_need=ProductRealizationOutputNeed.PREVIEW_WORLD)
    assert rejected.proposal.status is ProductRealizationStatus.CONTRACT_REJECTED
    assert payload is None


def test_batch_input_budget_counts_actual_float_wire_and_finish(tmp_path, monkeypatch):
    from CAD2BIM_backend.domain.product_realization import ProductRealizationOutputNeed
    from CAD2BIM_backend.domain.canonical import canonical_json
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import encode_json_frame
    request = _request(_mapped_binding(tmp_path), invocation=ProductRealizationInvocation(
        product_origin=(1.0, 0.0, 0.0), length_unit=IfcLengthUnit()))
    frame = realization_protocol.batch_invocation_frame(0, request, output_need=ProductRealizationOutputNeed.SOLVE_ONLY)
    actual = len(encode_json_frame(frame, limit=1024 * 1024)) + len(encode_json_frame({"finish": 1}, limit=1024))
    assert actual > len(canonical_json(frame).encode()) + 4
    monkeypatch.setattr(realization_protocol, "MAX_PRODUCT_BATCH_INPUT_BYTES", actual - 1)
    with pytest.raises(ValueError, match="byte budget"):
        realization_protocol.materialize_batch((request,), tmp_path / "rejected", output_needs=(ProductRealizationOutputNeed.SOLVE_ONLY,))
    monkeypatch.setattr(realization_protocol, "MAX_PRODUCT_BATCH_INPUT_BYTES", actual)
    accepted = tmp_path / "accepted"
    accepted.mkdir()
    realization_protocol.materialize_batch((request,), accepted, output_needs=(ProductRealizationOutputNeed.SOLVE_ONLY,))


@pytest.mark.asyncio
async def test_real_batch_reuses_mapped_template_with_fresh_helpers_and_one_codec(tmp_path, caplog, monkeypatch):
    import logging
    from CAD2BIM_backend.domain.product_realization import ProductRealizationOutputNeed
    from CAD2BIM_backend.infrastructure.product_realization_runtime.protocol import (
        _geometry_source_request_key,
    )
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    source = pack_source_bundle({
        "main.py": b"""from .helpers import fresh
def realize(ctx):
    if fresh()!=1:
        raise ValueError('helper state leaked')
    return {'status':'SOLVED','components':[{'component_key':'body'}],'fragments':[{'fragment_key':'body_shape','binding_key':'body_geometry'}]}
""",
        "helpers.py": b"def fresh(values=[]):\n    values.append(1)\n    return len(values)\n",
    })
    binding = _mapped_binding(tmp_path)
    requests = tuple(_request(binding, source=source, invocation=ProductRealizationInvocation(
        product_origin=(x, 0.0, 0.0), length_unit=IfcLengthUnit())) for x in (0.0, 2.0, 0.0))
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    codec_calls = []
    codec_inputs = []
    original_normalize = runtime._codec.normalize_batch

    async def counted_normalize(geometries):
        codec_calls.append(len(geometries))
        codec_inputs.extend(geometries)
        return await original_normalize(geometries)

    monkeypatch.setattr(runtime._codec, "normalize_batch", counted_normalize)
    try:
        prechecks = [value async for value in runtime.precheck_batch(requests)]
        values = [value async for value in runtime.instantiate_batch(requests, prechecks,
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)]
        assert len(values) == 3 and all(value.geometry is None for value in values)
        assert all(value.realization.proposal.status is ProductRealizationStatus.SOLVED for value in values)
        assert [value.realization.component_project_world_transforms[0].project_world_transform.translation_m[0]
                for value in values] == [0, 2, 0]
        assert all(value.instance_geometries['body_shape'].summary is None for value in values)
        assert all(value.instance_geometries['body_shape'].payload == binding.geometry.payload for value in values)
        assert len({id(value.realization) for value in values}) == 3
        receipts = [value.realization.instance_receipts for value in values]
        assert all(len(items) == 1 for items in receipts)
        assert len({id(items[0]) for items in receipts}) == 3
        for request, precheck, value, (receipt,) in zip(requests, prechecks, values, receipts, strict=True):
            assert value.realization.invocation == request.invocation
            assert receipt.model_dump(exclude={"geometry_sha256", "geometry_summary"}) == (
                precheck.interface_evaluations[0].model_dump())
            assert receipt.geometry_sha256 == value.instance_geometries['body_shape'].sha256
        assert len({_geometry_source_request_key(binding, items[0], exact=True)
                    for items in receipts}) == 1
        processes = [record.cad2bim_execution_phase for record in caplog.records
            if hasattr(record, 'cad2bim_execution_phase') and record.cad2bim_execution_phase['component']=='worker_runtime'
            and record.cad2bim_execution_phase['phase']=='process' and record.cad2bim_execution_phase['transition']=='end']
        product_events = [record['details']['child_timing'] for record in processes
            if any(event['phase'] in {'family_numeric_entry', 'reopen_mapped_template'}
                   for event in record['details']['child_timing'])]
        assert sum(event['phase'] == 'family_numeric_entry'
                   for events in product_events for event in events) == 3
        assert sum(event['phase'] == 'reopen_mapped_template'
                   for events in product_events for event in events) == 1
        instantiated = next(events for events in product_events
            if any(event['phase'] == 'reopen_mapped_template' for event in events))
        assert [event['details']['item_index'] for event in instantiated
                if event['phase'] == 'batch_item'] == [0, 1, 2]
        assert not any(event['phase'] == 'world_compose'
                       for events in product_events for event in events)
        assert codec_calls == [1]
        codec_input, = codec_inputs
        assert codec_input.format == binding.geometry.format
        assert codec_input.payload == binding.geometry.payload
        assert codec_input.include_summary is False
        batch_items = [event for event in instantiated if event['phase'] == 'batch_item']
        assert [
            [event['details']['binding_key'] for event in instantiated
             if event['phase'] == 'geometry_reused_source_receipt'
             and item['start_ns'] <= event['start_ns'] < event['end_ns'] <= item['end_ns']]
            for item in batch_items
        ] == [[], ['body_geometry'], ['body_geometry']]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_family_numeric_entry_failure_keeps_timing_and_item_identity(tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    source = pack_source_bundle({"main.py": b"def realize(ctx):\n    raise ValueError('numeric stop')\n"})
    request = _request(_mapped_binding(tmp_path), source=source)
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(ProductRealizationRuntimeValidationError) as failure:
            async for _ in runtime.realize_batch((request,), output_need=ProductRealizationOutputNeed.SOLVE_ONLY):
                pytest.fail("a failed numeric entry cannot yield a result")
        assert failure.value.code == "REALIZATION_EXECUTION_FAILED"
        assert failure.value.item_index == 0
        events = [event for record in caplog.records if hasattr(record, "cad2bim_execution_phase")
            for event in record.cad2bim_execution_phase.get("details", {}).get("child_timing", [])]
        numeric = [event for event in events if event["phase"] == "family_numeric_entry"]
        items = [event for event in events if event["phase"] == "batch_item"]
        assert len(numeric) == len(items) == 1
        assert items[0]["details"]["item_index"] == 0
        assert items[0]["start_ns"] <= numeric[0]["start_ns"] < numeric[0]["end_ns"] <= items[0]["end_ns"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("output_need", [ProductRealizationOutputNeed.PREVIEW_WORLD,
                                        ProductRealizationOutputNeed.LOCAL_FRAGMENTS])
async def test_real_batch_builds_each_named_function_and_preserves_template(tmp_path, caplog, monkeypatch, output_need):
    import logging
    from CAD2BIM_backend.domain.product_realization import ProductRealizationOutputNeed
    from CAD2BIM_backend.infrastructure.product_realization_runtime.protocol import (
        _geometry_source_request_key,
    )
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    requests = tuple(request.model_copy(update={"invocation": request.invocation.model_copy(
        update={"product_origin": (float(index), 0.0, 0.0)})})
        for index, request in enumerate(_scaling_batch_requests(tmp_path)))
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    codec_calls = []
    codec_inputs = []
    original_normalize = runtime._codec.normalize_batch

    async def counted_normalize(geometries):
        codec_calls.append(len(geometries))
        codec_inputs.extend(geometries)
        return await original_normalize(geometries)

    monkeypatch.setattr(runtime._codec, "normalize_batch", counted_normalize)
    try:
        prechecks = [value async for value in runtime.precheck_batch(requests)]
        values = [value async for value in runtime.instantiate_batch(requests, prechecks, output_need=output_need)]
        local = output_need is ProductRealizationOutputNeed.LOCAL_FRAGMENTS
        from CAD2BIM_backend.infrastructure.geometry_runtime.codec import read_geometry_payload
        assert [read_geometry_payload(value.instance_geometries['body_shape']).Volume()
                for value in values] == pytest.approx([0.125, 1, 0.125])
        assert [value.realization.component_project_world_transforms[0].project_world_transform.translation_m[0]
                for value in values] == [0, 1, 2]
        assert all(len(value.realization.instance_receipts) == 1 for value in values)
        assert all(value.realization.interface_evaluations == precheck.interface_evaluations
                   for value, precheck in zip(values, prechecks, strict=True))
        assert all((value.geometry is None) is local for value in values)
        if local:
            assert all(value.instance_geometries['body_shape'].summary is None for value in values)
            assert len({id(value.realization) for value in values}) == 3
            receipts = [value.realization.instance_receipts[0] for value in values]
            assert len({id(receipt) for receipt in receipts}) == 3
            for request, precheck, value, receipt in zip(requests, prechecks, values, receipts, strict=True):
                assert value.realization.invocation == request.invocation
                assert receipt.model_dump(exclude={"geometry_sha256", "geometry_summary"}) == (
                    precheck.interface_evaluations[0].model_dump())
                assert receipt.geometry_sha256 == value.instance_geometries['body_shape'].sha256
            source_keys = [_geometry_source_request_key(request.geometry_bindings[0], receipt, exact=True)
                           for request, receipt in zip(requests, receipts, strict=True)]
            assert source_keys[0] == source_keys[2] != source_keys[1]
            geometries = [value.instance_geometries['body_shape'] for value in values]
            assert geometries[0].payload == geometries[2].payload != geometries[1].payload
            assert codec_calls == [2]
            assert [(item.format, item.payload, item.include_summary) for item in codec_inputs] == [
                (geometry.format, geometry.payload, False) for geometry in geometries[:2]]
        else:
            assert [value.instance_geometries['body_shape'].summary.volume_m3
                    for value in values] == pytest.approx([0.125, 1, 0.125])
        events = [event for record in caplog.records if hasattr(record, 'cad2bim_execution_phase')
            for event in record.cad2bim_execution_phase.get('details',{}).get('child_timing',[])]
        assert len([event for event in events if event['phase']=='reopen_named_template']) == 1
        assert len([event for event in events if event['phase']=='compile_geometry']) == 1
        assert len([event for event in events if event['phase']=='geometry_build']) == (2 if local else 3)
        assert len([event for event in events if event['phase']=='world_compose']) == (0 if local else 3)
        cache_events = [event for event in events if event['phase'].startswith('geometry_local_result_cache_')]
        if local:
            assert [event['details']['cache_hit'] for event in cache_events] == [False, False]
            instantiated = next(record.cad2bim_execution_phase['details']['child_timing']
                for record in caplog.records if hasattr(record, 'cad2bim_execution_phase')
                and record.cad2bim_execution_phase['component'] == 'worker_runtime'
                and record.cad2bim_execution_phase['phase'] == 'process'
                and record.cad2bim_execution_phase['transition'] == 'end'
                and any(event['phase'] == 'reopen_named_template'
                    for event in record.cad2bim_execution_phase['details'].get('child_timing', [])))
            batch_items = [event for event in instantiated if event['phase'] == 'batch_item']
            assert [item['details']['item_index'] for item in batch_items] == [0, 1, 2]
            assert [
                [event['details']['binding_key'] for event in instantiated
                 if event['phase'] == 'geometry_reused_source_receipt'
                 and item['start_ns'] <= event['start_ns'] < event['end_ns'] <= item['end_ns']]
                for item in batch_items
            ] == [[], [], ['body_geometry']]
        else:
            assert [event['details']['cache_hit'] for event in cache_events] == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_batch_fatal_capability_failure_discards_worker_and_recovers_capacity(tmp_path, caplog):
    import logging
    from CAD2BIM_backend.domain.product_realization import ProductRealizationOutputNeed
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    requests = _scaling_batch_requests(tmp_path, fault=True)
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    yielded = []
    try:
        with pytest.raises(ProductRealizationRuntimeValidationError) as failure:
            prechecks = [value async for value in runtime.precheck_batch(requests)]
            async for value in runtime.instantiate_batch(requests, prechecks,
                    output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS):
                yielded.append(value)
        assert failure.value.code == 'REALIZATION_EXECUTION_FAILED'
        assert failure.value.item_index == 1
        assert len(yielded) == 1
        assert yielded[0].instance_geometries["body_shape"].summary is None
        events = [event for record in caplog.records if hasattr(record, 'cad2bim_execution_phase')
            if any(entry["phase"] == "family_numeric_entry" for entry in record.cad2bim_execution_phase.get('details',{}).get('child_timing',[]))
            for event in record.cad2bim_execution_phase.get('details',{}).get('child_timing',[])]
        assert [event['details']['item_index'] for event in events if event['phase']=='batch_item'] == [0,1,2]
        recovery_prechecks = [value async for value in runtime.precheck_batch(requests[:1])]
        recovered = [value async for value in runtime.instantiate_batch(requests[:1], recovery_prechecks,
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)]
        assert len(recovered) == 1
        geometry = recovered[0].instance_geometries['body_shape']
        assert geometry.summary is None
        from CAD2BIM_backend.infrastructure.geometry_runtime.codec import read_geometry_payload
        assert read_geometry_payload(geometry).Volume() == pytest.approx(0.125)
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["timeout", "partial_payload", "cancel", "codec", "between_items"])
async def test_batch_complete_prefix_survives_later_transport_failure(tmp_path, monkeypatch, failure_kind):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import adapter
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import WorkerProtocolError
    requests = _scaling_batch_requests(tmp_path)
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    prechecks = [item async for item in runtime.precheck_batch(requests)]
    assert len(prechecks) == len(requests)

    first_received = False
    class InterruptedChannel(adapter.AsyncWorkerChannel):
        partial_payload = False

        async def read_json(self, *, limit):
            nonlocal first_received
            value = await super().read_json(limit=limit)
            if value.get("status") == "succeeded" and value.get("item_index") == 0:
                first_received = True
            if value.get("status") == "succeeded" and value.get("item_index") == 1:
                if failure_kind == "cancel":
                    raise asyncio.CancelledError
                if failure_kind == "partial_payload":
                    self.partial_payload = True
                else:
                    raise WorkerSupervisorError("WORKER_TIMEOUT", "later item timed out",
                        execution_started=True, resource_kind="wall")
            return value

        async def read_exact(self, size):
            if self.partial_payload:
                await super().read_exact(max(1, size // 2))
                raise WorkerProtocolError("payload interrupted halfway")
            return await super().read_exact(size)

    monkeypatch.setattr(adapter, "AsyncWorkerChannel", InterruptedChannel)
    if failure_kind == "between_items":
        from CAD2BIM_backend.infrastructure.worker_runtime.supervisor import WorkerInvocationControl
        def remaining_cpu(_self):
            if first_received:
                raise WorkerSupervisorError("WORKER_SERVICE_UNAVAILABLE", "worker disappeared before dispatch",
                    execution_started=True)
            return 900.0
        monkeypatch.setattr(WorkerInvocationControl, "remaining_cpu_seconds", property(remaining_cpu))
    codec_calls = []
    original_normalize = runtime._codec.normalize_batch

    async def normalize(requests):
        codec_calls.append(len(requests))
        if failure_kind == "codec":
            raise GeometryRuntimeValidationError("independent codec rejected the prefix")
        return await original_normalize(requests)

    monkeypatch.setattr(runtime._codec, "normalize_batch", normalize)
    yielded = []
    expected = (asyncio.CancelledError if failure_kind == "cancel" else
        ProductRealizationRuntimeResourceLimitError if failure_kind == "timeout" else
        ProductRealizationRuntimeUnavailableError if failure_kind == "between_items" else
        ProductRealizationRuntimeValidationError)
    try:
        with pytest.raises(expected) as failure:
            async for value in runtime.instantiate_batch(requests, prechecks,
                    output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS):
                yielded.append(value)
        if failure_kind in {"cancel", "codec"}:
            assert yielded == []
            assert codec_calls == ([] if failure_kind == "cancel" else [1])
            if failure_kind == "codec":
                assert failure.value.diagnostic["reason"] == "independent codec rejected the prefix"
                assert isinstance(failure.value.__context__, GeometryRuntimeValidationError)
                assert isinstance(failure.value.__cause__, WorkerSupervisorError)
        else:
            assert len(yielded) == 1 and codec_calls == [1]
            assert failure.value.item_index == (None if failure_kind == "between_items" else 1)
            from CAD2BIM_backend.infrastructure.geometry_runtime.codec import read_geometry_payload
            assert read_geometry_payload(yielded[0].instance_geometries["body_shape"]).Volume() == pytest.approx(0.125)
        # Closing/throwing never retains scratch or monopolizes the host lease.
        assert not runtime._capacity.locked()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_batch_stuck_second_item_is_terminated_and_prefix_is_validated(tmp_path):
    requests = _scaling_batch_requests(tmp_path)
    source = pack_source_bundle({"main.py": b"""def realize(ctx):
    if ctx.param('scale') == 2:
        while True:
            pass
    return {'status':'SOLVED','components':[{'component_key':'body'}],'fragments':[{'fragment_key':'body_shape','binding_key':'body_geometry'}]}
"""})
    requests = tuple(request.model_copy(update={"source_bundle": source,
        "source_bundle_sha256": hashlib.sha256(source).hexdigest()}) for request in requests)

    class ShortSecondItem(WorkerProcessSupervisor):
        async def run(self, spec, *, budget, deadline, interaction=None, on_guard_registered=None):
            async def controlled(control):
                class Invocations:
                    count = 0

                    @property
                    def remaining_cpu_seconds(self):
                        return control.remaining_cpu_seconds

                    @asynccontextmanager
                    async def invocation(self, *, wall_seconds, cpu_seconds):
                        self.count += 1
                        async with control.invocation(wall_seconds=0.15 if self.count == 2 else wall_seconds,
                                cpu_seconds=cpu_seconds):
                            yield
                await interaction(Invocations())
            return await super().run(spec, budget=budget, deadline=deadline,
                interaction=controlled if interaction is not None else None,
                on_guard_registered=on_guard_registered)

    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity", supervisor=ShortSecondItem())
    yielded = []
    started = time.monotonic()
    try:
        with pytest.raises(ProductRealizationRuntimeResourceLimitError) as failure:
            async for value in runtime.realize_batch(requests, output_need=ProductRealizationOutputNeed.SOLVE_ONLY):
                yielded.append(value)
        assert len(yielded) == 1
        assert yielded[0].realization.proposal.status is ProductRealizationStatus.SOLVED
        assert yielded[0].geometry is None and yielded[0].instance_geometries is None
        assert failure.value.item_index == 1
        assert failure.value.diagnostic["resource_kind"] == "wall"
        assert time.monotonic() - started < 20
        assert not runtime._capacity.locked()
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_kind", ["wall", "cpu", "wrong_finish_count", "boolean_finish_count", "trailing_finish"])
async def test_batch_budget_soft_finish_yields_only_completed_prefix(tmp_path, monkeypatch, resource_kind):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import adapter
    from CAD2BIM_backend.infrastructure.worker_runtime.supervisor import WorkerInvocationControl
    requests = _scaling_batch_requests(tmp_path)
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    prechecks = [item async for item in runtime.precheck_batch(requests)]
    assert len(prechecks) == len(requests)

    completed = False
    class CompletedChannel(adapter.AsyncWorkerChannel):
        async def write(self, frame):
            if json.loads(frame[4:]) == {"finish": 1}:
                if resource_kind == "wrong_finish_count":
                    frame = adapter.encode_json_frame({"finish": 2}, limit=1024)
                elif resource_kind == "boolean_finish_count":
                    frame = adapter.encode_json_frame({"finish": True}, limit=1024)
                elif resource_kind == "trailing_finish":
                    frame += b"x"
            await super().write(frame)

        async def read_json(self, *, limit):
            nonlocal completed
            value = await super().read_json(limit=limit)
            if value.get("status") == "succeeded":
                completed = True
                if resource_kind == "wall":
                    # The current item keeps its original timeout; the next
                    # dispatch no longer fits a full item in the remaining wall.
                    monkeypatch.setattr(adapter, "PRODUCT_REALIZATION_WALL_SECONDS", 900.0)
            return value

    monkeypatch.setattr(adapter, "AsyncWorkerChannel", CompletedChannel)
    if resource_kind != "wall":
        monkeypatch.setattr(WorkerInvocationControl, "remaining_cpu_seconds",
            property(lambda _self: 30.0 if completed else 900.0))
    yielded = []
    try:
        invalid_finish = resource_kind not in {"wall", "cpu"}
        expected = ProductRealizationRuntimeValidationError if invalid_finish else ProductRealizationRuntimeResourceLimitError
        with pytest.raises(expected) as failure:
            async for value in runtime.instantiate_batch(requests, prechecks,
                    output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS):
                yielded.append(value)
        assert len(yielded) == 1
        assert failure.value.item_index is None
        if invalid_finish:
            assert "finish count mismatch" in str(failure.value)
        else:
            assert failure.value.diagnostic == {"phase": "batch_dispatch", "reason": "batch_budget_exhausted",
                "completed_count": 1, "remaining_count": 2, "resource_kind": resource_kind}
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["startup", "execution"])
async def test_batch_host_timeout_reports_the_active_item_without_a_result(tmp_path, monkeypatch, failure_phase):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import adapter

    root = None

    class ReadyChannel:
        bytes_read = 0
        bytes_written = 0

        def __init__(self, *_fds):
            pass

        async def read_json(self, *, limit):
            del limit
            if failure_phase == "startup":
                raise TimeoutError("ready timeout")
            return {"status": "ready"}

    class Supervision:
        remaining_cpu_seconds = 900.0

        @asynccontextmanager
        async def invocation(self, **_budget):
            assert _budget == {"wall_seconds": 30.0, "cpu_seconds": 30.0}
            yield

    class TimeoutDuringItem:
        async def run(self, spec, *, budget, deadline, interaction, on_guard_registered=None):
            nonlocal root
            assert budget.wall_seconds == budget.cpu_seconds == 900.0
            assert 845 < deadline - time.monotonic() <= 855
            root = spec.scratch
            assert on_guard_registered is None  # no child process exists in this transport double
            return await interaction(Supervision())

    def timeout_item(*_args, **_kwargs):
        raise WorkerSupervisorError("WORKER_TIMEOUT", "active item timed out",
            execution_started=True, resource_kind="wall")

    monkeypatch.setattr(adapter, "AsyncWorkerChannel", ReadyChannel)
    monkeypatch.setattr(adapter, "batch_invocation_frame", timeout_item)
    runtime = OcpCadQueryProductRealizationRuntime(
        capacity_root=tmp_path / "capacity", supervisor=TimeoutDuringItem(),
    )
    yielded = []
    try:
        with pytest.raises(ProductRealizationRuntimeResourceLimitError) as failure:
            async for value in runtime.realize_batch(
                (_request(_mapped_binding(tmp_path)),), output_need=ProductRealizationOutputNeed.SOLVE_ONLY,
            ):
                yielded.append(value)
        assert yielded == []
        assert failure.value.item_index == (None if failure_phase == "startup" else 0)
        assert failure.value.retryable is False
        assert failure.value.diagnostic["phase"] == failure_phase
        if failure_phase == "startup":
            assert failure.value.diagnostic["limit"] == 30.0
            assert failure.value.diagnostic["observed"] >= 0
        assert root is not None and not root.exists()
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("received_bytes,over_limit", [
    (64 * 1024**2 + 1, False),
    (256 * 1024**2, False),
    (256 * 1024**2 + 1, True),
])
async def test_batch_host_aggregate_output_boundary(tmp_path, monkeypatch, received_bytes, over_limit):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import adapter

    root = None

    class ReachedResultValidation(Exception):
        pass

    class CountedChannel:
        bytes_read = 0
        bytes_written = 0

        def __init__(self, *_fds):
            self.reads = 0

        async def read_json(self, *, limit):
            self.reads += 1
            if self.reads == 1:
                return {"status": "ready"}
            self.bytes_read = received_bytes
            return {"status": "succeeded"}

        async def write(self, frame):
            self.bytes_written += len(frame)

    class Supervision:
        remaining_cpu_seconds = 900.0

        @asynccontextmanager
        async def invocation(self, **_budget):
            yield

    class CountedTransport:
        async def run(self, spec, *, budget, deadline, interaction, on_guard_registered=None):
            nonlocal root
            root = spec.scratch
            assert on_guard_registered is None  # no child process exists in this counter double
            return await interaction(Supervision())

    def reached_validation(*_args, **_kwargs):
        raise ReachedResultValidation

    # Exercise the real Host counter guard without allocating a 256 MiB payload.
    monkeypatch.setattr(adapter, "AsyncWorkerChannel", CountedChannel)
    monkeypatch.setattr(adapter, "validate_batch_result_frame", reached_validation)
    runtime = OcpCadQueryProductRealizationRuntime(
        capacity_root=tmp_path / "capacity", supervisor=CountedTransport())
    try:
        error = ProductRealizationRuntimeValidationError if over_limit else ReachedResultValidation
        with pytest.raises(error) as caught:
            async for _ in runtime.realize_batch(
                (_request(_mapped_binding(tmp_path)),), output_need=ProductRealizationOutputNeed.SOLVE_ONLY,
            ):
                pytest.fail("counter-only transport cannot produce a product result")
        if over_limit:
            assert "wire output byte budget exceeded" in str(caught.value)
        assert root is not None and not root.exists()
    finally:
        await runtime.close()


def test_batch_child_unavailable_keeps_only_a_legal_child_item_index():
    from CAD2BIM_backend.infrastructure.product_realization_runtime.adapter import _raise_child_failure

    with pytest.raises(ProductRealizationRuntimeUnavailableError) as attributed:
        _raise_child_failure({"code": "REALIZATION_RUNTIME_FAILED", "item_index": 2,
                              "message": "worker exited"})
    assert attributed.value.item_index == 2
    with pytest.raises(ProductRealizationRuntimeUnavailableError) as unknown:
        _raise_child_failure({"code": "REALIZATION_RUNTIME_FAILED", "item_index": True,
                              "message": "worker exited"})
    assert unknown.value.item_index is None


@pytest.mark.parametrize("code,phase", [
    ("REALIZATION_SOURCE_INVALID", "code_validation"),
    ("REALIZATION_EXECUTION_FAILED", "interface_evaluation"),
])
def test_child_source_diagnostic_survives_bounded_terminal_and_batch_error(code, phase):
    import io
    from CAD2BIM_backend.infrastructure.product_realization_runtime.child import (
        _ChildError, _failure_payload, _write_failure,
    )
    from CAD2BIM_backend.infrastructure.product_realization_runtime.adapter import _raise_child_failure
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import encode_json_frame

    diagnostic = {
        "exception_type": "ValueError", "message": '\\"数值不合法' * 500,
        "source_path": "helpers.py", "function": "evaluate", "line": 123,
        "column": 7, "end_column": 29, "source_line": '    raise ValueError("数值不合法")' * 100,
        "code_sha256": "a" * 64, "binding_key": "body_geometry",
        "source_bundle_sha256": "b" * 64,
    }
    failure = _ChildError(code, diagnostic["message"], phase, diagnostic=diagnostic)
    stream = io.BytesIO()
    _write_failure(stream, failure, started=True)
    terminal = json.loads(stream.getvalue())
    frame = {**_failure_payload(failure), "item_index": 2}
    encode_json_frame(frame, limit=4096)
    assert terminal["diagnostic"] == frame["diagnostic"]
    assert terminal["diagnostic"]["message_truncated"] is True
    assert terminal["diagnostic"]["source_line_truncated"] is True
    for key in ("source_path", "function", "line", "column", "end_column",
                "code_sha256", "binding_key", "source_bundle_sha256"):
        assert terminal["diagnostic"][key] == diagnostic[key]
    with pytest.raises(ProductRealizationRuntimeValidationError) as caught:
        _raise_child_failure(json.loads(json.dumps(frame)))
    assert caught.value.code == code and caught.value.phase == phase
    assert caught.value.item_index == 2
    assert caught.value.diagnostic == terminal["diagnostic"]


def test_interface_execution_error_keeps_its_original_source_diagnostic(monkeypatch):
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child
    from CAD2BIM_backend.infrastructure.geometry_runtime import modeling_execution
    from CAD2BIM_backend.domain.product_realization import ProductRealizationOutputNeed
    failure = child._ChildError("REALIZATION_EXECUTION_FAILED", "interface source failed", "interface_evaluation",
        diagnostic={"source_path": "interfaces.py", "line": 11, "binding_key": "body_geometry"})
    def fail_interfaces(proposal):
        raise failure
    monkeypatch.setattr(modeling_execution, "execute_family_program", lambda *args, **kwargs: {
        "status": "SOLVED", "components": [{"component_key": "body"}],
        "fragments": [{"fragment_key": "body_shape", "binding_key": "body_geometry"}],
    })
    def normalize_proposal(request, proposal, **kwargs):
        document = proposal.model_dump()
        document["fragments"][0]["geometry_parameters"] = {}
        return child.SolvedProductRealizationProposal.model_validate(document)
    monkeypatch.setattr(child, "validate_product_realization_proposal", normalize_proposal)
    context = SimpleNamespace(parameter_rules={}, evaluate_interfaces=fail_interfaces)
    request = SimpleNamespace(invocation=SimpleNamespace(realization_kind="realize"))
    with pytest.raises(child._ChildError) as caught:
        child._evaluate_invocation(request, None, context, output_need=ProductRealizationOutputNeed.INTERFACES)
    assert caught.value is failure


@pytest.mark.asyncio
async def test_single_geometry_capability_failure_is_classified_as_execution(tmp_path):
    request = _scaling_batch_requests(tmp_path, fault=True)[1]
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(ProductRealizationRuntimeValidationError) as failure:
            await runtime.realize(request)
        assert failure.value.code == "REALIZATION_EXECUTION_FAILED"
        assert failure.value.phase == "geometry_build"
        assert "undeclared Shape operation: missing_api" in failure.value.message
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_batch_cancellation_releases_capacity_and_private_scratch(tmp_path, monkeypatch):
    from CAD2BIM_backend.domain.product_realization import ProductRealizationOutputNeed
    from CAD2BIM_backend.infrastructure.product_realization_runtime import adapter
    ready = asyncio.Event()
    roots = []
    original_mkdtemp = adapter.tempfile.mkdtemp
    def capture_root(*args, **kwargs):
        path = original_mkdtemp(*args, **kwargs)
        roots.append(Path(path))
        return path
    class ReadyChannel(adapter.AsyncWorkerChannel):
        async def read_json(self, *, limit):
            value = await super().read_json(limit=limit)
            if value.get("status") == "ready":
                ready.set()
            return value
    monkeypatch.setattr(adapter, "AsyncWorkerChannel", ReadyChannel)
    monkeypatch.setattr(adapter.tempfile, "mkdtemp", capture_root)
    binding = _mapped_binding(tmp_path)
    request = _request(binding, source=pack_source_bundle({"main.py": b"def realize(ctx):\n    while True:\n        pass\n"}))
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    iterator = runtime.realize_batch((request,), output_need=ProductRealizationOutputNeed.SOLVE_ONLY)
    task = asyncio.create_task(anext(iterator))
    try:
        await asyncio.wait_for(ready.wait(), timeout=20)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert roots and all(not root.exists() for root in roots)
        values = [value async for value in runtime.realize_batch((_request(binding),), output_need=ProductRealizationOutputNeed.SOLVE_ONLY)]
        assert len(values) == 1 and values[0].geometry is None and values[0].instance_geometries is None
    finally:
        task.cancel()
        await iterator.aclose()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_input", [True, False])
async def test_real_batch_corrupt_frame_is_attributed_and_stops_child(tmp_path, monkeypatch, corrupt_input):
    from CAD2BIM_backend.domain.product_realization import ProductRealizationOutputNeed
    from CAD2BIM_backend.infrastructure.product_realization_runtime import adapter
    from CAD2BIM_backend.infrastructure.worker_runtime.protocol import encode_json_frame
    received_result = False
    class CorruptChannel(adapter.AsyncWorkerChannel):
        async def write(self, payload):
            if corrupt_input:
                value = json.loads(payload[4:])
                if "item_index" in value:
                    value["item_index"] = 99
                    payload = encode_json_frame(value, limit=1024 * 1024)
            await super().write(payload)
        async def read_json(self, *, limit):
            nonlocal received_result
            value = await super().read_json(limit=limit)
            if value.get("status") == "succeeded" and "item_index" in value:
                received_result = True
                value["item_index"] = 99
            return value
    monkeypatch.setattr(adapter, "AsyncWorkerChannel", CorruptChannel)
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(ProductRealizationRuntimeValidationError) as failure:
            async for _ in runtime.realize_batch((_request(_mapped_binding(tmp_path)),), output_need=ProductRealizationOutputNeed.SOLVE_ONLY):
                pytest.fail("corrupt batch must not yield")
        assert failure.value.item_index == 0
        assert failure.value.code == ("REALIZATION_BINDING_INVALID" if corrupt_input else "REALIZATION_OUTPUT_INVALID")
        # A rejected input produces no result. A corrupted response was already
        # produced by the child; an abort need not flush its shutdown timings.
        assert received_result is (not corrupt_input)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_batch_infeasible_continues_without_world_or_payloads(tmp_path, caplog):
    import logging
    from CAD2BIM_backend.domain.product_realization import ProductRealizationOutputNeed
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    requests = _scaling_batch_requests(tmp_path)
    source = pack_source_bundle({"main.py": b"""def realize(ctx):
    if ctx.param('scale')==2:
        return {'status':'INFEASIBLE','violations':[{'violation_key':'constraint','message':'explicit input constraint'}]}
    return {'status':'SOLVED','components':[{'component_key':'body'}],'fragments':[{'fragment_key':'body_shape','binding_key':'body_geometry'}]}
"""})
    requests = tuple(request.model_copy(update={"source_bundle": source, "source_bundle_sha256": hashlib.sha256(source).hexdigest()}) for request in requests)
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        values = [value async for value in runtime.realize_batch(requests, output_need=ProductRealizationOutputNeed.SOLVE_ONLY)]
        assert [value.realization.proposal.status.value for value in values] == ['SOLVED', 'INFEASIBLE', 'SOLVED']
        assert all(value.geometry is None and value.instance_geometries is None for value in values)
        events = [event for record in caplog.records if hasattr(record, 'cad2bim_execution_phase')
            for event in record.cad2bim_execution_phase.get('details',{}).get('child_timing',[])]
        assert len([event for event in events if event['phase']=='family_numeric_entry']) == 3
        assert not any(event['phase']=='geometry_build' for event in events)
        assert not any(event['phase']=='world_compose' for event in events)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_standard_model_uses_independent_entry_and_explicit_product_origin(tmp_path):
    from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import reopen_user_shape

    family = ProductFamilyContract.model_validate(
        {
            **_family().model_dump(),
            "standard_model": {},
        }
    )
    source = pack_source_bundle(
        {
            "main.py": b"""def realize(ctx):
    return {"status": "INFEASIBLE", "violations": [{"violation_key": "fixture_conflict", "message": "Controlled test: connection unavailable."}]}

def standard_model(ctx):
    return {"status": "SOLVED", "components": [{"component_key": "body"}],
            "fragments": [{"fragment_key": "body_shape", "binding_key": "body_geometry"}]}
"""
        }
    )
    anchor = RigidTransform3D(translation_m=(0.25, 0, 0))
    binding = _mapped_binding(tmp_path).model_copy(
        update={"interfaces": (current_model_interface(frame=anchor),)}
    )
    request = _family_request(
        family,
        binding,
        source,
        invocation=ProductRealizationInvocation(
            realization_kind="standard_model",
            product_origin=(10.0, 20.0, 30.0),
            length_unit=IfcLengthUnit(),
        ),
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        connected = await runtime.realize(
            request.model_copy(update={"invocation": ProductRealizationInvocation(
                product_origin=(10.0, 20.0, 30.0), length_unit=IfcLengthUnit())})
        )
        execution = await runtime.realize_geometry(request)
    finally:
        await runtime.close()
    assert connected.proposal.status is ProductRealizationStatus.INFEASIBLE
    assert execution.realization.proposal.status is ProductRealizationStatus.SOLVED
    assert execution.realization.component_project_world_transforms[
        0
    ].project_world_transform.translation_m == (10.0, 20.0, 30.0)
    shape = reopen_user_shape(execution.geometry.payload)
    # The fixture box is centered at its local origin; its world center follows
    # the explicit product-origin transform exactly.
    assert shape.Center().toTuple() == pytest.approx((10.0, 20.0, 30.0))
    assert shape.Volume() == pytest.approx(1 * 0.5 * 0.25)


@pytest.mark.asyncio
async def test_standard_model_rejects_removed_v2_root_field(tmp_path):
    family = ProductFamilyContract.model_validate(
        {
            **_family().model_dump(),
            "standard_model": {},
        }
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        source = pack_source_bundle(
            {
                "main.py": b"""def standard_model(ctx):
    return {"status": "SOLVED", "root_world_transform": {"translation_m": [100, 0, 0]},
            "components": [{"component_key": "body"}],
            "fragments": [{"fragment_key": "body_shape", "binding_key": "body_geometry"}]}
"""
            }
        )
        request = _family_request(
            family,
            _mapped_binding(tmp_path),
            source,
            invocation=ProductRealizationInvocation(
                realization_kind="standard_model", product_origin=(0.0, 0.0, 0.0),
                length_unit=IfcLengthUnit()),
        )
        execution = await runtime.realize_geometry(request)
        assert execution.realization.proposal.status is ProductRealizationStatus.CONTRACT_REJECTED
        assert "root_world_transform" in execution.realization.proposal.rejection.message
        assert execution.geometry is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("include_instances", [False, True])
async def test_preview_returns_one_independently_verified_canonical_compound(
    tmp_path: Path,
    include_instances: bool,
) -> None:
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    request = _request(_mapped_binding(tmp_path))

    execution = await runtime.realize_geometry(request, include_instances=include_instances)

    assert execution.realization.proposal.status is ProductRealizationStatus.SOLVED
    assert execution.geometry is not None
    assert execution.geometry.format is CanonicalGeometryFormat.OCCT_BREP_V1
    assert hashlib.sha256(execution.geometry.payload).hexdigest() == execution.geometry.sha256
    assert execution.geometry.summary.topology.solid_count == 1
    assert len(execution.realization.instance_receipts) == 1
    if include_instances:
        assert execution.instance_geometries is not None
        receipt = execution.realization.instance_receipts[0]
        actual = execution.instance_geometries[receipt.fragment_key]
        assert actual.sha256 == receipt.geometry_sha256
        assert actual.payload == request.geometry_bindings[0].geometry.payload
    else:
        assert execution.instance_geometries is None


@pytest.mark.asyncio
@pytest.mark.parametrize("include_instances", [False, True])
async def test_non_solved_preview_never_returns_candidate_geometry(
    tmp_path: Path,
    include_instances: bool,
) -> None:
    source = pack_source_bundle(
        {
            "main.py": (
                b"def realize(ctx):\n"
                b"    return {\n"
                b"        'status': 'AMBIGUOUS',\n"
                b"        'ambiguities': [{\n"
                b"            'ambiguity_key': 'clocking',\n"
                b"            'message': 'two orientations remain',\n"
                b"        }],\n"
                b"    }\n"
            )
        }
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    execution = await runtime.realize_geometry(
        _request(_mapped_binding(tmp_path), source=source), include_instances=include_instances
    )

    assert execution.realization.proposal.status is ProductRealizationStatus.AMBIGUOUS
    assert execution.realization.instance_receipts == ()
    assert execution.geometry is None
    assert execution.instance_geometries is None


@pytest.mark.asyncio
@pytest.mark.parametrize("include_instances", [False, True])
async def test_preview_composes_fragments_as_a_non_fused_assembly(
    tmp_path: Path,
    include_instances: bool,
) -> None:
    family = ProductFamilyContract(
        product_structure_kind=ProductFamilyStructureKind.SEMANTIC_ASSEMBLY,
        components=(
            ProductFamilyComponentContract(
                component_key="assembly",
                ifc_class=ProductFamilyIfcClass.ELEMENT_ASSEMBLY,
            ),
            ProductFamilyComponentContract(
                component_key="left",
                ifc_class=ProductFamilyIfcClass.VALVE,
                parent_component_key="assembly",
            ),
            ProductFamilyComponentContract(
                component_key="right",
                fixed_local_transform=RigidTransform3D(translation_m=(0.25, 0, 0)),
                ifc_class=ProductFamilyIfcClass.DISCRETE_ACCESSORY,
                parent_component_key="assembly",
            ),
        ),
        geometry_fragments=(
            ProductFamilyGeometryFragmentContract(
                fragment_key="left_shape",
                owner_component_key="left",
                binding_options=(ProductFamilyFragmentBindingOption(
                    binding_key="left_geometry", parameter_sources={"overrides": {}}),),
            ),
            ProductFamilyGeometryFragmentContract(
                fragment_key="right_shape",
                owner_component_key="right",
                binding_options=(ProductFamilyFragmentBindingOption(
                    binding_key="right_geometry", parameter_sources={"overrides": {}}),),
            ),
        ),
    )
    first = _mapped_binding(tmp_path).model_copy(update={"binding_key": "left_geometry"})
    second = _mapped_binding(tmp_path).model_copy(update={"binding_key": "right_geometry"})
    source = pack_source_bundle(
        {
            "main.py": (
                b"""def realize(ctx):
    return {
        'status': 'SOLVED',
        'components': [
            {'component_key': 'assembly'},
            {'component_key': 'left'},
            {'component_key': 'right'},
        ],
        'fragments': [
            {'fragment_key': 'left_shape', 'binding_key': 'left_geometry'},
            {'fragment_key': 'right_shape', 'binding_key': 'right_geometry'},
        ],
    }
"""
            )
        }
    )
    request = ProductRealizationRequest(
        family_contract=family,
        family_contract_digest=sha256_digest(family),
        source_bundle=source,
        source_bundle_sha256=hashlib.sha256(source).hexdigest(),
        geometry_bindings=(first, second),
        invocation=ProductRealizationInvocation(
            product_origin=(0.0, 0.0, 0.0), length_unit=IfcLengthUnit()),
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    execution = await runtime.realize_geometry(request, include_instances=include_instances)

    assert execution.geometry is not None
    assert execution.geometry.summary.topology.solid_count == 2
    assert len(execution.realization.instance_receipts) == 2
    if include_instances:
        assert execution.instance_geometries is not None
        assert len(execution.instance_geometries) == 2
        # Component placement must not be applied to local geometry a second time.
        assert {g.payload for g in execution.instance_geometries.values()} == {
            first.geometry.payload
        }
        assert all(
            g.summary == first.geometry.summary for g in execution.instance_geometries.values()
        )
    else:
        assert execution.instance_geometries is None


@pytest.mark.asyncio
async def test_realization_context_exposes_fixed_policy_and_read_only_origin(
    tmp_path: Path,
) -> None:
    source = pack_source_bundle(
        {
            "main.py": (
                b"""def realize(ctx):
    policy = ctx.policy()
    if policy != {
        'mode': 'preview', 'unit': 'METRE',
        'frame_tolerance_m': 1e-6,
        'angular_tolerance_rad': 1e-6,
    }:
        raise ValueError('unexpected realization policy')
    origin = list(ctx.product_origin())
    origin[0] = 9.0
    if ctx.product_origin() != (1.0, 2.0, 3.0):
        raise ValueError('origin leaked mutation')
    return {
        'status': 'SOLVED',
        'components': [{'component_key': 'body'}],
        'fragments': [{
            'fragment_key': 'body_shape',
            'binding_key': 'body_geometry',

        }],
    }
"""
            )
        }
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    result = await runtime.realize(
        _request(
            _mapped_binding(tmp_path),
            source=source,
            invocation=ProductRealizationInvocation(
                product_origin=(1.0, 2.0, 3.0), length_unit=IfcLengthUnit()),
        )
    )

    assert result.component_project_world_transforms[
        0
    ].project_world_transform.translation_m == (1.0, 2.0, 3.0)


@pytest.mark.asyncio
async def test_mapped_binding_must_pass_neutral_canonical_reopen(
    tmp_path: Path,
) -> None:
    binding = _mapped_binding(tmp_path)
    invalid = binding.model_copy(
        update={
            "geometry": binding.geometry.model_copy(
                update={"content_type": "application/octet-stream"}
            )
        }
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    with pytest.raises(ProductRealizationRuntimeValidationError) as captured:
        await runtime.realize(_request(invalid))

    assert captured.value.code == "REALIZATION_BINDING_INVALID"
    assert captured.value.phase == "input_validation"
    assert "content type" in captured.value.message


@pytest.mark.asyncio
async def test_modeling_function_is_built_after_numeric_solution_in_the_same_child(
    tmp_path: Path,
) -> None:
    function_source = pack_source_bundle(
        {'main.py': b"import cadquery as cq\n\ndef build(ctx):\n    return cq.Workplane('XY').circle(ctx.param('radius')).extrude(ctx.param('height'))\n",
'interfaces.py': b"def evaluate(ctx):\n    ctx.define_interface(key='datum', origin_m=(0, 0, 0), axis=(0, 0, 1), reference_direction=(1, 0, 0))\n"}
    )
    binding = ResolvedModelingFunctionBinding(
        resource_kind=GeometryResourceKind.MODELING_FUNCTION,
        binding_key="body_geometry",
        geometry_content_digest="1" * 64,
        source_bundle=function_source,
        source_bundle_sha256=hashlib.sha256(function_source).hexdigest(),
        parameter_schema={
            "type": "object",
            "properties": {
                "height": {"type": "number", "minimum": 0.01},
                "radius": {"type": "number", "minimum": 0.01},
            },
            "required": ["height", "radius"],
            "additionalProperties": False,
        },
    )
    source = pack_source_bundle(
        {
            "main.py": (
                b"""def realize(ctx):
    return {
        'status': 'SOLVED',
        'components': [{'component_key': 'body'}],
        'fragments': [{
            'fragment_key': 'body_shape',
            'binding_key': 'body_geometry',

        }],
    }
"""
            )
        }
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    try:
        execution = await runtime.realize_geometry(
            _request(
                binding,
                source=source,
                parameter_sources={
                    "height": {"source_kind": "fixed", "value": 0.4},
                    "radius": {"source_kind": "fixed", "value": 0.1},
                },
            )
        )
    finally:
        await runtime.close()
    result = execution.realization

    assert result.instance_receipts[0].normalized_parameters == {
        "height": 0.4,
        "radius": 0.1,
    }
    assert "modeling_execution_contract" not in result.model_dump()
    assert "expected_modeling_execution_contract" not in type(binding).model_fields
    assert result.instance_receipts[0].geometry_summary.volume_m3 == pytest.approx(
        0.0125663706,
        rel=1e-6,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_domain", ["geometry", "family"])
async def test_nested_bundle_execution_diagnostic_uses_own_exact_source(
    tmp_path: Path,
    failure_domain: str,
) -> None:
    geometry_helper = b"def fail():\n    raise ValueError('bound geometry failure')\n"
    family_helper = b"def fail():\n    raise ValueError('family failure')\n"
    function_source = pack_source_bundle(
        {'main.py': b'from .helpers import fail\ndef build(ctx):\n    return fail()\n',
'helpers.py': geometry_helper,
'interfaces.py': b'def evaluate(ctx):\n    pass\n'}
    )
    binding = ResolvedModelingFunctionBinding(
        binding_key="body_geometry",
        geometry_content_digest="1" * 64,
        source_bundle=function_source,
        source_bundle_sha256=hashlib.sha256(function_source).hexdigest(),
        parameter_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    )
    statement = ("{'status':'SOLVED','components':[{'component_key':'body'}],'fragments':[{'fragment_key':'body_shape','binding_key':'body_geometry'}]}"
                 if failure_domain == "geometry" else "fail()")
    family_source = pack_source_bundle(
        {
            "main.py": f"from .helpers import fail\ndef realize(ctx):\n    return {statement}\n".encode(),
            "helpers.py": family_helper,
        }
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(ProductRealizationRuntimeValidationError) as caught:
            await runtime.realize(_request(binding, source=family_source))
        assert caught.value.code == "REALIZATION_EXECUTION_FAILED"
        diagnostic = caught.value.diagnostic
        expected = geometry_helper if failure_domain == "geometry" else family_helper
        assert diagnostic["code_sha256"] == hashlib.sha256(expected).hexdigest(), diagnostic
        assert diagnostic["source_path"] == "helpers.py"
        assert diagnostic["line"] == 2
        assert diagnostic["source_line"] == expected.decode().splitlines()[1]
        if failure_domain == "geometry":
            assert diagnostic["binding_key"] == "body_geometry"
            assert diagnostic["source_bundle_sha256"] == binding.source_bundle_sha256
        else:
            assert "binding_key" not in diagnostic
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_fragment_parameter_constraint_diagnostic_preflights_and_recovers(
    tmp_path: Path,
) -> None:
    function_source = pack_source_bundle(
        {'main.py': b"import cadquery as cq\ndef build(ctx):\n    return cq.Workplane('XY').box(ctx.param('length'), 0.2, 0.2)\n",
'interfaces.py': b"def evaluate(ctx):\n    ctx.define_interface(key='datum', origin_m=(0, 0, 0), axis=(0, 0, 1), reference_direction=(1, 0, 0))\n"}
    )
    binding = ResolvedModelingFunctionBinding(
        resource_kind=GeometryResourceKind.MODELING_FUNCTION,
        binding_key="body_geometry",
        geometry_content_digest="1" * 64,
        source_bundle=function_source,
        source_bundle_sha256=hashlib.sha256(function_source).hexdigest(),
        parameter_schema={
            "type": "object",
            "properties": {"length": {"type": "number", "maximum": 1}},
            "required": ["length"],
            "additionalProperties": False,
        },
    )

    def program() -> bytes:
        return pack_source_bundle(
            {
                "main.py": (
                    "def realize(ctx):\n"
                    "    return {'status': 'SOLVED',\n"
                    "            'components': [{'component_key': 'body'}],\n"
                    "            'fragments': [{'fragment_key': 'body_shape', 'binding_key': 'body_geometry'}]}\n"
                ).encode()
            }
        )

    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        rejected = await runtime.realize(
            _request(
                binding,
                source=program(),
                parameter_sources={"length": {"source_kind": "fixed", "value": 1.0000000000000002}},
            )
        )
        # Fixed values now belong to the saved mapping. The existing Geometry
        # normalizer rejects their range as a contract-rejected proposal.
        assert rejected.proposal.status is ProductRealizationStatus.CONTRACT_REJECTED
        assert rejected.instance_receipts == ()
        message = rejected.proposal.rejection.message
        assert "invalid_path=length" in message
        assert "error_type=less_than_equal" in message
        assert "constraint=maximum=1" in message
        assert "1.0000000000000002" not in message
        assert "input_value" not in message
        recovered = await runtime.realize(
            _request(
                binding,
                source=program(),
                parameter_sources={"length": {"source_kind": "fixed", "value": 1.0}},
            )
        )
        assert recovered.proposal.status is ProductRealizationStatus.SOLVED
        assert recovered.instance_receipts[0].normalized_parameters == {"length": 1.0}
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_modeling_function_receipt_exposes_independently_evaluated_interfaces(
    tmp_path: Path,
) -> None:
    function_source = pack_source_bundle(
        {'main.py': b"import cadquery as cq\n\ndef build(ctx):\n    length = ctx.param('length_m')\n    return cq.Workplane('XY').box(length, 0.2, 0.2)\n",
'interfaces.py': b"def evaluate(ctx):\n    ctx.define_interface(key='datum', origin_m=(0, 0, 0), axis=(0, 0, 1), reference_direction=(1, 0, 0))\n    length = ctx.param('length_m')\n    ctx.define_interface(key='outlet', origin_m=(length, 0.0, 0.0), axis=(1.0, 0.0, 0.0), reference_direction=(0.0, 0.0, 1.0), profile={'kind': 'circular', 'diameter_m': 0.2})\n"}
    )
    binding = ResolvedModelingFunctionBinding(
        resource_kind=GeometryResourceKind.MODELING_FUNCTION,
        binding_key="body_geometry",
        geometry_content_digest="3" * 64,
        source_bundle=function_source,
        source_bundle_sha256=hashlib.sha256(function_source).hexdigest(),
        parameter_schema={
            "type": "object",
            "properties": {"length_m": {"type": "number", "minimum": 0.01}},
            "required": ["length_m"],
            "additionalProperties": False,
        },
    )
    source = pack_source_bundle(
        {
            "main.py": (
                b"""def realize(ctx):
    return {
        'status': 'SOLVED',
        'components': [{'component_key': 'body'}],
        'fragments': [{'fragment_key': 'body_shape', 'binding_key': 'body_geometry'}],
    }
"""
            )
        }
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    result = await runtime.realize(
        _request(
            binding,
            source=source,
            parameter_sources={"length_m": {"source_kind": "fixed", "value": 0.7}},
        )
    )

    receipt = result.instance_receipts[0]
    assert receipt.interfaces[1].interface_key == "outlet"
    assert receipt.interfaces[1].local_frame.translation_m == (0.7, 0.0, 0.0)
    assert result.component_project_world_transforms[0].project_world_transform.translation_m == (
        0.0,
        0.0,
        0.0,
    )


@pytest.mark.asyncio
async def test_model_interface_uses_selected_scaled_receipt_and_one_world_transform_chain(
    tmp_path: Path,
) -> None:
    path = tmp_path / "offset-body.brep"
    exporters.export(
        cq.Workplane("XY").box(1.0, 0.5, 0.25).translate((0.75, 0.25, 0.5)).val(),
        str(path),
        exportType="BREP",
    )
    geometry = normalize_geometry(
        GeometryNormalizeRequest(
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            source_format=GeometrySourceFormat.OCCT_BREP,
            payload=path.read_bytes(),
        )
    )
    binding = ResolvedMappedGeometryBinding(
        binding_key="body_geometry",
        geometry_content_digest=geometry.sha256,
        geometry=geometry,
        interfaces=(
            {
                "interface_key": "outlet",
                "local_frame": {"translation_m": (0.5, 0.0, 0.0)},
                "profile": {"kind": "circular", "diameter_m": 0.2},
            },
        ),
    )
    family = ProductFamilyContract.model_validate(
        {
            "product_structure_kind": "semantic_assembly",
            "components": [
                {
                    "component_key": "assembly",
                    "ifc_class": "IfcElementAssembly",
                },
                {
                    "component_key": "housing",
                    "ifc_class": "IfcValve",
                    "parent_component_key": "assembly",
                    "fixed_local_transform": {
                        "translation_m": (1, 0, 0),
                        "rotation": ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
                    },
                },
                {
                    "component_key": "body",
                    "ifc_class": "IfcDiscreteAccessory",
                    "parent_component_key": "housing",
                    "fixed_local_transform": {"translation_m": (2, 0, 0)},
                },
            ],
            "geometry_fragments": [
                {
                    "fragment_key": "housing_shape",
                    "owner_component_key": "housing",
                    "binding_options": [{"binding_key": "body_geometry", "parameter_sources": {"overrides": {}}}],
                },
                {
                    "fragment_key": "body_selected",
                    "binding_options": [{"binding_key": "body_geometry", "parameter_sources": {"overrides": {}},
                                         "uniform_scale_source": {"source_kind": "fixed", "value": 2.0}}],
                    "fixed_local_transform": {"translation_m": (3, 0, 0)},
                    "owner_component_key": "body",
                },
            ],
            "interfaces": [
                {
                    "interface_key": "family_outlet",
                    "owner_component_key": "body",
                    "interface_kind": "external",
                    "source": {
                        "source_kind": "model_interface",
                        "fragment_key": "body_selected",
                        "model_interface_key": "outlet",
                    },
                }
            ],
        }
    )
    source = pack_source_bundle(
        {
            "main.py": (
                b"""def t(x, rotation=None):
    value = {'translation_m': [x, 0.0, 0.0]}
    if rotation is not None:
        value['rotation'] = rotation
    return value

def realize(ctx):
    return {
        'status': 'SOLVED',
        'components': [
            {'component_key': 'assembly'},
            {'component_key': 'housing'},
            {'component_key': 'body'},
        ],
        'fragments': [
            {'fragment_key': 'housing_shape', 'binding_key': 'body_geometry'},
            {'fragment_key': 'body_selected', 'binding_key': 'body_geometry',  'uniform_scale': 2.0},
        ],
    }
"""
            )
        }
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    execution = await runtime.realize_geometry(
        _family_request(
            family,
            binding,
            source,
            invocation=ProductRealizationInvocation(
                product_origin=(100.0, 0.0, 0.0), length_unit=IfcLengthUnit(),
            ),
        )
    )

    result = execution.realization
    await runtime.close()
    original, scaled = result.instance_receipts
    world = result.interface_project_world_frames[0]
    assert original.uniform_scale == 1.0
    assert "uniform_scale" not in original.model_dump(mode="json")
    assert original.geometry_sha256 == binding.geometry.sha256
    assert scaled.uniform_scale == 2.0
    assert scaled.geometry_sha256 != binding.geometry.sha256
    assert scaled.geometry_summary.volume_m3 == pytest.approx(
        original.geometry_summary.volume_m3 * 8.0
    )
    assert scaled.geometry_summary.bounds.minimum_m == pytest.approx(
        tuple(value * 2.0 for value in original.geometry_summary.bounds.minimum_m)
    )
    assert scaled.geometry_summary.bounds.maximum_m == pytest.approx(
        tuple(value * 2.0 for value in original.geometry_summary.bounds.maximum_m)
    )
    original_center = tuple(
        (low + high) / 2.0
        for low, high in zip(
            original.geometry_summary.bounds.minimum_m,
            original.geometry_summary.bounds.maximum_m,
            strict=True,
        )
    )
    scaled_center = tuple(
        (low + high) / 2.0
        for low, high in zip(
            scaled.geometry_summary.bounds.minimum_m,
            scaled.geometry_summary.bounds.maximum_m,
            strict=True,
        )
    )
    assert original_center == pytest.approx((0.75, 0.25, 0.5))
    assert scaled_center == pytest.approx(tuple(value * 2.0 for value in original_center))
    assert scaled.interfaces[0].local_frame.translation_m == (1.0, 0.0, 0.0)
    assert world.interface_key == "family_outlet"
    assert world.source is not None and world.source.source_kind == "model_interface"
    assert world.component_local_frame is not None
    assert world.component_local_frame.translation_m == (4.0, 0.0, 0.0)
    assert world.project_world_frame.translation_m == pytest.approx((101.0, 6.0, 0.0))
    assert world.project_world_frame.rotation == (
        (0.0, -1.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    assert world.profile is not None
    assert world.profile.model_dump(mode="json") == {
        "kind": "circular",
        "diameter_m": 0.4,
    }


@pytest.mark.asyncio
async def test_model_interface_source_consumes_independently_evaluated_function_interfaces(
    tmp_path: Path,
) -> None:
    function_source = pack_source_bundle(
        {'main.py': b"import cadquery as cq\n\ndef build(ctx):\n    length = ctx.param('length_m')\n    return cq.Workplane('XY').box(length, 0.2, 0.1)\n",
'interfaces.py': b"def evaluate(ctx):\n    ctx.define_interface(key='datum', origin_m=(0, 0, 0), axis=(0, 0, 1), reference_direction=(1, 0, 0))\n    length = ctx.param('length_m')\n    ctx.define_interface(key='outlet', origin_m=(length, 0.0, 0.0), axis=(1.0, 0.0, 0.0), reference_direction=(0.0, 0.0, 1.0), profile={'kind': 'rectangle', 'width_m': 0.2, 'height_m': 0.1})\n"}
    )
    binding = ResolvedModelingFunctionBinding(
        resource_kind=GeometryResourceKind.MODELING_FUNCTION,
        binding_key="body_geometry",
        geometry_content_digest="7" * 64,
        source_bundle=function_source,
        source_bundle_sha256=hashlib.sha256(function_source).hexdigest(),
        parameter_schema={
            "type": "object",
            "properties": {"length_m": {"type": "number", "minimum": 0.01}},
            "required": ["length_m"],
            "additionalProperties": False,
        },
    )
    family = ProductFamilyContract.model_validate(
        {
            **current_family_contract(
                parameter_sources={"length_m": {"source_kind": "fixed", "value": 0.7}}
            ).model_dump(mode="json", by_alias=True),
            "interfaces": [
                {
                    "interface_key": "family_outlet",
                    "owner_component_key": "body",
                    "interface_kind": "external",
                    "source": {
                        "source_kind": "model_interface",
                        "fragment_key": "body_shape",
                        "model_interface_key": "outlet",
                    },
                }
            ],
        }
    )
    source = pack_source_bundle(
        {
            "main.py": (
                b"""def realize(ctx):
    return {
        'status': 'SOLVED',
        'components': [{'component_key': 'body'}],
        'fragments': [{'fragment_key': 'body_shape', 'binding_key': 'body_geometry'}],
    }
"""
            )
        }
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    result = await runtime.realize(_family_request(family, binding, source))

    assert result.instance_receipts[0].interfaces[1].interface_key == "outlet"
    assert result.interface_project_world_frames[0].interface_key == "family_outlet"
    assert result.interface_project_world_frames[0].project_world_frame.translation_m == (
        0.7,
        0.0,
        0.0,
    )
    assert result.interface_project_world_frames[0].profile is not None
    assert result.interface_project_world_frames[0].profile.model_dump(mode="json") == {
        "kind": "rectangle",
        "width_m": 0.2,
        "height_m": 0.1,
    }


@pytest.mark.asyncio
async def test_explicit_model_interface_frame_relations_pass_backend_diagnostics(
    tmp_path: Path,
) -> None:
    request = _model_interface_pair_request(
        tmp_path,
        right_frame={
            "rotation": (
                (0.0, -1.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 0.0, 1.0),
            )
        },
        relation={
            "direction_relation": "same",
            "orientation_requirement": "axis_only",
            "profile_requirement": "equal",
        },
        include_opposed_full_pair=True,
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    result = await runtime.realize(request)

    assert result.frame_check_status == "PASS"
    assert tuple(item.check_key for item in result.frame_diagnostics) == (
        "aligned_pair",
        "opposed_pair",
    )
    axis_only, opposed_full = (item.evaluation for item in result.frame_diagnostics)
    assert axis_only.satisfied is True
    assert axis_only.direction_residual_rad == pytest.approx(0.0)
    assert axis_only.orientation_residual_rad is None
    assert axis_only.profile_satisfied is True
    assert opposed_full.satisfied is True
    assert opposed_full.direction_residual_rad == pytest.approx(0.0)
    assert opposed_full.orientation_residual_rad == pytest.approx(0.0)
    assert opposed_full.profile_satisfied is True


@pytest.mark.asyncio
async def test_explicit_frame_failure_keeps_numeric_diagnostics_without_preview_geometry(
    tmp_path: Path,
) -> None:
    request = _model_interface_pair_request(
        tmp_path,
        right_frame={"translation_m": (0.01, 0.0, 0.0)},
        relation={
            "direction_relation": "same",
            "orientation_requirement": "full_frame",
            "profile_requirement": "equal",
        },
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    execution = await runtime.realize_geometry(request)

    assert execution.geometry is None
    assert execution.realization.proposal.status is ProductRealizationStatus.SOLVED
    assert execution.realization.frame_check_status == "FAIL"
    assert len(execution.realization.frame_diagnostics) == 1
    diagnostic = execution.realization.frame_diagnostics[0]
    assert diagnostic.check_kind == "interface_coincidence"
    assert diagnostic.evaluation.satisfied is False
    assert diagnostic.evaluation.origin_distance_m == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_model_interface_source_rejects_missing_actual_interface(tmp_path: Path) -> None:
    request = _model_interface_pair_request(
        tmp_path,
        right_frame={},
        relation={
            "direction_relation": "same",
            "orientation_requirement": "axis_only",
        },
        include_right_interface=False,
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    with pytest.raises(ProductRealizationRuntimeValidationError) as captured:
        await runtime.realize(request)

    assert captured.value.code == "REALIZATION_BINDING_INVALID"
    assert "the selected Geometry has no referenced model interface" in captured.value.message


@pytest.mark.asyncio
async def test_modeling_function_instance_uses_shared_canonical_reopen(
    tmp_path: Path,
) -> None:
    function_source = pack_source_bundle(
        {'main.py': b"import cadquery as cq\n\ndef build(ctx):\n    import math\n    path = (cq.Workplane('XZ').moveTo(0.0, 0.0)\n            .lineTo(0.161, 0.0).lineTo(0.161, 0.16)\n            .lineTo(0.46, 0.16))\n    angle = math.degrees(math.atan2(\n        ctx.param('routing_y_m'), ctx.param('routing_x_m')))\n    return (cq.Workplane('YZ').circle(0.006)\n            .sweep(path, transition='round')\n            .rotate((0, 0, 0), (1, 0, 0), angle).val())\n",
'interfaces.py': b"def evaluate(ctx):\n    ctx.define_interface(key='datum', origin_m=(0, 0, 0), axis=(0, 0, 1), reference_direction=(1, 0, 0))\n"}
    )
    binding = ResolvedModelingFunctionBinding(
        resource_kind=GeometryResourceKind.MODELING_FUNCTION,
        binding_key="body_geometry",
        geometry_content_digest="2" * 64,
        source_bundle=function_source,
        source_bundle_sha256=hashlib.sha256(function_source).hexdigest(),
        parameter_schema={
            "type": "object",
            "properties": {
                "routing_x_m": {"type": "number", "minimum": 0.01},
                "routing_y_m": {"type": "number", "minimum": 0.01},
            },
            "required": ["routing_x_m", "routing_y_m"],
            "additionalProperties": False,
        },
    )
    source = pack_source_bundle(
        {
            "main.py": (
                b"""def realize(ctx):
    return {
        'status': 'SOLVED',
        'components': [{'component_key': 'body'}],
        'fragments': [{
            'fragment_key': 'body_shape',
            'binding_key': 'body_geometry',

        }],
    }
"""
            )
        }
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    request = _request(
        binding,
        source=source,
        parameter_sources={
            "routing_x_m": {"source_kind": "fixed", "value": 1.5},
            "routing_y_m": {"source_kind": "fixed", "value": 1.2},
        },
    )
    execution = await runtime.realize_geometry(request)
    replay = await runtime.realize_geometry(request)

    assert execution.geometry is not None
    assert replay.geometry is not None
    assert replay.realization.component_project_world_transforms == execution.realization.component_project_world_transforms
    assert replay.realization.instance_receipts == execution.realization.instance_receipts
    assert replay.geometry.sha256 == execution.geometry.sha256
    assert execution.realization.instance_receipts[0].geometry_summary == (
        execution.geometry.summary
    )


@pytest.mark.asyncio
async def test_local_function_construction_only_follows_preselected_parametric_route(tmp_path: Path, caplog) -> None:
    import logging
    caplog.set_level(logging.INFO, logger="CAD2BIM.execution")
    function_source = pack_source_bundle({
        "main.py": b"import cadquery as cq\ndef build(ctx):\n    return cq.Workplane('XY').circle(ctx.param('routing_x_m')).extrude(ctx.param('routing_y_m')).val()\n",
        "interfaces.py": b"def evaluate(ctx):\n    ctx.define_interface(key='datum', origin_m=(0, 0, 0), axis=(0, 0, 1), reference_direction=(1, 0, 0))\n",
    })
    binding = ResolvedModelingFunctionBinding(
        resource_kind=GeometryResourceKind.MODELING_FUNCTION,
        binding_key="body_geometry",
        geometry_content_digest=hashlib.sha256(function_source).hexdigest(),
        source_bundle=function_source,
        source_bundle_sha256=hashlib.sha256(function_source).hexdigest(),
        parameter_schema={
            "type": "object",
            "properties": {
                "routing_x_m": {"type": "number", "minimum": 0.01},
                "routing_y_m": {"type": "number", "minimum": 0.01},
            },
            "required": ["routing_x_m", "routing_y_m"],
            "additionalProperties": False,
        },
    )
    source = pack_source_bundle({"main.py": b"def realize(ctx):\n    return {'status':'SOLVED','components':[{'component_key':'body'}],'fragments':[{'fragment_key':'body_shape','binding_key':'body_geometry'}]}\n"})
    request = _request(binding, source=source, parameter_sources={
        "routing_x_m": {"source_kind": "fixed", "value": 0.2},
        "routing_y_m": {"source_kind": "fixed", "value": 0.5},
    })
    param_request = request.model_copy(update={"geometry_bindings": (
        binding.model_copy(update={"ifc_representation_route": IfcGeometryRepresentationRoute.PARAMETRIC}),
    )})
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        executions = []
        # A representation route belongs to the exact batch closure. Exercise
        # each route in its own valid batch instead of mixing two closures.
        for selected in (request, param_request):
            requests = (selected, selected)
            prechecks = [value async for value in runtime.precheck_batch(requests)]
            executions.extend([value async for value in runtime.instantiate_batch(
                requests, prechecks, output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)])
    finally:
        await runtime.close()
    assert len(executions) == 4
    assert all(value.instance_constructions == {} for value in executions[:2])
    trace = executions[2].instance_constructions["body_shape"]
    reused = executions[3].instance_constructions["body_shape"]
    assert reused == trace and reused is not trace
    assert any(node.op == "extrude" and node.kwargs["$receiver_state"]["pending_wires"]
               for node in trace.nodes)
    assert len({value.instance_geometries["body_shape"].sha256 for value in executions}) == 1
    # Returned construction DTOs are detached even though their cached source is
    # immutable; one row must never mutate another row's encoding facts.
    trace.nodes[0].kwargs["unrelated_test_marker"] = True
    assert "unrelated_test_marker" not in reused.nodes[0].kwargs
    events = [event for record in caplog.records if hasattr(record, 'cad2bim_execution_phase')
        for event in record.cad2bim_execution_phase.get('details',{}).get('child_timing',[])]
    assert sum(event['phase'] == 'geometry_build' for event in events) == 2
    assert [event['details']['cache_hit'] for event in events
            if event['phase'].startswith('geometry_local_result_cache_')] == [False, False]
    assert sum(event['phase'] == 'geometry_reused_source_receipt' for event in events) == 2


@pytest.mark.parametrize("conflict", ["system_scale", "retired_parameters"])
def test_modeling_function_system_scale_is_rejected_before_geometry(
    tmp_path: Path, monkeypatch, conflict,
) -> None:
    from CAD2BIM_backend.infrastructure.product_realization_runtime import child

    base = _scaling_batch_requests(tmp_path)[0]
    family_document = base.family_contract.model_dump(mode="json", by_alias=True)
    if conflict == "system_scale":
        family_document["geometry_fragments"][0]["binding_options"][0]["uniform_scale_source"] = {
            "source_kind": "fixed", "value": 2.0}
    family = ProductFamilyContract.model_validate(family_document)
    program_source = b"""def realize(ctx):
    return {'status':'SOLVED', 'components':[{'component_key':'body'}],
            'fragments':[{'fragment_key':'body_shape','binding_key':'body_geometry',

                          'uniform_scale':2.0}]}
"""
    if conflict == "retired_parameters":
        program_source = program_source.replace(b"'binding_key':'body_geometry',",
            b"'binding_key':'body_geometry', 'geometry_parameters':{'scale':2.71828},").replace(
            b"'uniform_scale':2.0", b"'uniform_scale':1.0"
        )
    source = pack_source_bundle({"main.py": program_source})
    request = base.model_copy(update={
        "family_contract": family,
        "family_contract_digest": sha256_digest(family),
        "source_bundle": source,
        "source_bundle_sha256": hashlib.sha256(source).hexdigest(),
    })
    program = child._compile_family(source, "test_function_scale", "realize")

    class ForbiddenPreparation:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("invalid function system scale reached Geometry")

    monkeypatch.setattr(child, "_PreparedGeometryBindings", ForbiddenPreparation)
    rejected, payload = _evaluate_current_invocation(
        request, program, child._ProductRealizationContext(request),
        output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)
    assert rejected.proposal.status is ProductRealizationStatus.CONTRACT_REJECTED
    assert payload is None
    message = rejected.proposal.rejection.message
    if conflict == "system_scale":
        assert "uniform_scale" in message
    else:
        assert "geometry_parameters" in message
        assert "extra_forbidden" in message
        assert "2.71828" not in message
        assert len(message.encode()) < 2_000


@pytest.mark.asyncio
async def test_runtime_requires_the_fixed_realize_entry(tmp_path: Path) -> None:
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    request = _request(_mapped_binding(tmp_path), source=_program(entry="solve"))

    with pytest.raises(ProductRealizationRuntimeValidationError) as captured:
        await runtime.realize(request)

    assert captured.value.code == "REALIZATION_SOURCE_INVALID"
    assert captured.value.phase == "code_validation"


@pytest.mark.asyncio
async def test_output_validation_rejects_duplicate_fragment_before_geometry(tmp_path: Path) -> None:
    source = pack_source_bundle(
        {
            "main.py": (
                b"""def realize(ctx):
    return {
        'status': 'SOLVED',
        'components': [{'component_key': 'body'}],
        'fragments': [
            {'fragment_key': 'body_shape', 'binding_key': 'body_geometry'},
            {'fragment_key': 'body_shape', 'binding_key': 'body_geometry'},
        ],
    }
"""
            )
        }
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    result = await runtime.realize(_request(_mapped_binding(tmp_path), source=source))

    assert result.proposal.status is ProductRealizationStatus.CONTRACT_REJECTED
    assert result.proposal.rejection.violation_key == "proposal_contract_rejected"
    assert result.instance_receipts == ()


@pytest.mark.asyncio
async def test_output_validation_diagnostic_omits_values_and_pydantic_urls(tmp_path: Path) -> None:
    source = pack_source_bundle(
        {
            "main.py": (
                b"""def realize(ctx):
    return {
        'status': 'SOLVED',
        'components': [{'component_key': 'body'}],
        'fragments': [{'fragment_key': 'body_shape', 'binding_key': 'body_geometry'}],
        'interfaces': [{'interface_key': 'inlet', 'local_frame': {'origin_m': [0, 0, 0]}, 'connection_profile': {'profile_kind': 'circular', 'outer_diameter_m': 0.2}}],
    }
"""
            )
        }
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    result = await runtime.realize(_request(_mapped_binding(tmp_path), source=source))

    assert result.proposal.status is ProductRealizationStatus.CONTRACT_REJECTED
    message = result.proposal.rejection.message
    assert "interfaces: extra_forbidden" in message
    assert "errors.pydantic.dev" not in message
    assert "input_value" not in message


@pytest.mark.asyncio
async def test_two_fragments_of_same_binding_receive_distinct_fragment_receipts(
    tmp_path: Path,
) -> None:
    base = _family()
    family = base.model_copy(
        update={
            "geometry_fragments": (
                base.geometry_fragments[0],
                ProductFamilyGeometryFragmentContract(
                    fragment_key="body_shape_second",
                    binding_options=(ProductFamilyFragmentBindingOption(
                        binding_key="body_geometry", parameter_sources={"overrides": {}}),),
                    owner_component_key="body",
                ),
            )
        }
    )
    source = pack_source_bundle(
        {
            "main.py": (
                b"""def realize(ctx):
    return {
        'status': 'SOLVED',
        'components': [{'component_key': 'body'}],
        'fragments': [
            {'fragment_key': 'body_shape', 'binding_key': 'body_geometry'},
            {'fragment_key': 'body_shape_second', 'binding_key': 'body_geometry'},
        ],
    }
"""
            )
        }
    )
    request = ProductRealizationRequest(
        family_contract=family,
        family_contract_digest=sha256_digest(family),
        source_bundle=source,
        source_bundle_sha256=hashlib.sha256(source).hexdigest(),
        geometry_bindings=(_mapped_binding(tmp_path),),
        invocation=ProductRealizationInvocation(
            product_origin=(0.0, 0.0, 0.0), length_unit=IfcLengthUnit()),
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    result = await runtime.realize(request)
    assert [item.fragment_key for item in result.instance_receipts] == [
        "body_shape", "body_shape_second"]


@pytest.mark.asyncio
async def test_ambiguous_solver_may_use_but_does_not_publish_candidate_geometry(
    tmp_path: Path,
) -> None:
    source = pack_source_bundle(
        {
            "main.py": (
                b"def realize(ctx):\n"
                b"    return {\n"
                b"        'status': 'AMBIGUOUS',\n"
                b"        'ambiguities': [{\n"
                b"            'ambiguity_key': 'clocking',\n"
                b"            'message': 'two clocking orientations remain',\n"
                b"        }],\n"
                b"    }\n"
            )
        }
    )
    family = _family()
    request = ProductRealizationRequest(
        family_contract=family,
        family_contract_digest=sha256_digest(family),
        source_bundle=source,
        source_bundle_sha256=hashlib.sha256(source).hexdigest(),
        geometry_bindings=(_mapped_binding(tmp_path),),
        invocation=ProductRealizationInvocation(
            product_origin=(0.0, 0.0, 0.0), length_unit=IfcLengthUnit()),
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")

    result = await runtime.realize(request)

    assert result.proposal.status is ProductRealizationStatus.AMBIGUOUS
    assert result.instance_receipts == ()


@pytest.mark.asyncio
async def test_exact_instance_geometry_spans_codec_batches_and_stays_local(tmp_path: Path) -> None:
    family = ProductFamilyContract(
        product_structure_kind=ProductFamilyStructureKind.SINGLE_PRODUCT,
        components=(
            ProductFamilyComponentContract(
                component_key="body", ifc_class=ProductFamilyIfcClass.VALVE
            ),
        ),
        geometry_fragments=tuple(
            ProductFamilyGeometryFragmentContract(
                fragment_key=f"part_{index}",
                owner_component_key="body",
                binding_options=(ProductFamilyFragmentBindingOption(
                    binding_key="body_geometry", parameter_sources={"overrides": {}}),),
                fixed_local_transform=RigidTransform3D(translation_m=(index * 0.25, 0, 0)),
            )
            for index in range(9)
        ),
    )
    source = pack_source_bundle(
        {
            "main.py": b"""def realize(ctx):
    fragments = []
    for index in range(9):
        fragments.append({"fragment_key": "part_" + str(index), "binding_key": "body_geometry"})
    return {"status": "SOLVED",
            "components": [{"component_key": "body"}], "fragments": fragments}
"""
        }
    )
    binding = _mapped_binding(tmp_path)
    request = _family_request(
        family,
        binding,
        source,
        invocation=ProductRealizationInvocation(
            product_origin=(2.0, 3.0, 4.0), length_unit=IfcLengthUnit(),
        ),
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        execution = await runtime.realize_geometry(request, include_instances=True)
    finally:
        await runtime.close()
    assert len(execution.instance_geometries) == 9
    assert execution.geometry.summary.topology.solid_count == 9
    assert all(
        value.payload == binding.geometry.payload
        for value in execution.instance_geometries.values()
    )
    assert all(
        value.summary == binding.geometry.summary
        for value in execution.instance_geometries.values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper", ["payload", "fragment_key", "duplicate", "trailing", "summary", "missing",
               "proposal", "checked_interfaces"]
)
async def test_exact_instance_output_rejects_wrong_receipt_or_bytes_and_cleans_scratch(
    tmp_path: Path,
    tamper: str,
    monkeypatch,
) -> None:
    from CAD2BIM_backend.infrastructure.product_realization_runtime import adapter

    class TamperedChannel(adapter.AsyncWorkerChannel):
        world_bytes_remaining = 0
        instance_bytes_remaining = 0

        async def read_json(self, *, limit):
            response = await super().read_json(limit=limit)
            if response.get("status") != "succeeded" or response.get("output_need") != ProductRealizationOutputNeed.PREVIEW_WORLD.value:
                return response
            geometries = response["geometries"]
            assert [entry["fragment_key"] for entry in geometries] == [None, "body_shape"]
            self.world_bytes_remaining = geometries[0]["geometry"]["byte_length"]
            self.instance_bytes_remaining = geometries[1]["geometry"]["byte_length"]
            if tamper == "fragment_key":
                geometries[1]["fragment_key"] = "wrong_fragment"
            elif tamper == "duplicate":
                geometries.append(geometries[1].copy())
            elif tamper == "missing":
                geometries.pop()
            elif tamper == "summary":
                geometries[1]["geometry"]["summary"]["volume_m3"] = 99.0
            elif tamper == "trailing":
                geometries[1]["geometry"]["byte_length"] -= 1
            elif tamper == "proposal":
                response["result"]["proposal"]["components"][0]["local_transform"]["translation_m"][0] = 0.1
            elif tamper == "checked_interfaces":
                response["result"]["interface_evaluations"][0]["interfaces"][0]["local_frame"]["translation_m"][0] = 0.1
            return response

        async def read_exact(self, size):
            content = await super().read_exact(size)
            if self.world_bytes_remaining:
                self.world_bytes_remaining -= len(content)
                return content
            if self.instance_bytes_remaining:
                self.instance_bytes_remaining -= len(content)
                if tamper == "payload":
                    altered = bytearray(content)
                    altered[-1] ^= 1
                    return bytes(altered)
            return content

    monkeypatch.setattr(adapter, "AsyncWorkerChannel", TamperedChannel)
    supervisor = _TrackingSupervisor()
    runtime = OcpCadQueryProductRealizationRuntime(
        capacity_root=tmp_path / "capacity", supervisor=supervisor
    )
    try:
        with pytest.raises(ProductRealizationRuntimeValidationError) as failure:
            await runtime.realize_geometry(
                _request(_mapped_binding(tmp_path)), include_instances=True
            )
        assert failure.value.code == "REALIZATION_OUTPUT_INVALID"
    finally:
        await runtime.close()
    assert not supervisor.scratch.exists()
    assert supervisor.calls == 2


class _CrashedSupervisor:
    def __init__(self) -> None:
        self.scratch: Path | None = None

    async def run(
        self,
        spec: WorkerLaunchSpec,
        *,
        budget: WorkerProcessBudget,
        deadline: float,
        interaction=None,
        on_guard_registered=None,
    ) -> WorkerTerminal:
        del budget, deadline, interaction, on_guard_registered
        self.scratch = spec.scratch
        return WorkerTerminal(
            returncode=-11,
            status_payload=None,
            stdout=b"",
            stderr=b"",
            execution_started=True,
            observations=WorkerProcessObservations(),
        )


class _TimeoutSupervisor:
    def __init__(self) -> None:
        self.scratch: Path | None = None

    async def run(
        self,
        spec: WorkerLaunchSpec,
        *,
        budget: WorkerProcessBudget,
        deadline: float,
        interaction=None,
        on_guard_registered=None,
    ) -> WorkerTerminal:
        del budget, deadline, interaction, on_guard_registered
        self.scratch = spec.scratch
        raise WorkerSupervisorError(
            "WORKER_TIMEOUT",
            "simulated product realization timeout",
            execution_started=True,
        )


class _BlockingSupervisor:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.never = asyncio.Event()
        self.scratch: Path | None = None

    async def run(
        self,
        spec: WorkerLaunchSpec,
        *,
        budget: WorkerProcessBudget,
        deadline: float,
        interaction=None,
        on_guard_registered=None,
    ) -> WorkerTerminal:
        del budget, deadline, interaction, on_guard_registered
        self.scratch = spec.scratch
        self.entered.set()
        await self.never.wait()
        raise AssertionError("unreachable")


class _TrackingSupervisor:
    def __init__(self) -> None:
        self.delegate = WorkerProcessSupervisor()
        self.calls = 0
        self.scratch: Path | None = None

    async def run(
        self,
        spec: WorkerLaunchSpec,
        *,
        budget: WorkerProcessBudget,
        deadline: float,
        interaction=None,
        on_guard_registered=None,
    ) -> WorkerTerminal:
        self.calls += 1
        self.scratch = spec.scratch
        return await self.delegate.run(spec, budget=budget, deadline=deadline,
            interaction=interaction, on_guard_registered=on_guard_registered)


async def _wait_for_supervisor_entry_or_task(task: asyncio.Task, entered: asyncio.Event) -> None:
    waiter = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait((task, waiter), timeout=20.0,
                                     return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task  # propagate an early Worker failure instead of hanging on entered
            pytest.fail("operation finished before the blocking supervisor was entered")
        if waiter not in done:
            pytest.fail("blocking supervisor was not entered within 20 seconds")
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ("crash", "timeout"))
async def test_preview_worker_failure_is_unavailable_and_cleans_private_scratch(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    supervisor = _CrashedSupervisor() if failure_kind == "crash" else _TimeoutSupervisor()
    runtime = OcpCadQueryProductRealizationRuntime(
        capacity_root=tmp_path / "capacity",
        supervisor=supervisor,  # type: ignore[arg-type]
    )

    with pytest.raises(ProductRealizationRuntimeUnavailableError):
        await runtime.realize_geometry(_request(_mapped_binding(tmp_path)))

    assert supervisor.scratch is not None and not supervisor.scratch.exists()


@pytest.mark.asyncio
async def test_preview_cancellation_cleans_private_scratch(tmp_path: Path) -> None:
    supervisor = _BlockingSupervisor()
    runtime = OcpCadQueryProductRealizationRuntime(
        capacity_root=tmp_path / "capacity",
        supervisor=supervisor,  # type: ignore[arg-type]
    )
    task = asyncio.create_task(runtime.realize_geometry(_request(_mapped_binding(tmp_path))))
    await _wait_for_supervisor_entry_or_task(task, supervisor.entered)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert supervisor.scratch is not None and not supervisor.scratch.exists()


@pytest.mark.asyncio
async def test_preview_cancellation_after_product_child_cleans_both_worker_layers_and_capacity(
    tmp_path: Path,
) -> None:
    capacity = HostWorkerCapacity(
        max_concurrent_workers=1,
        capacity_root=tmp_path / "capacity",
    )
    product_supervisor = _TrackingSupervisor()
    codec_supervisor = _BlockingSupervisor()
    codec = OcpGeometryCodecExecutor(
        budget=GEOMETRY_CODEC_BUDGET,
        supervisor=codec_supervisor,  # type: ignore[arg-type]
        capacity=capacity,
    )
    runtime = OcpCadQueryProductRealizationRuntime(
        supervisor=product_supervisor,
        capacity=capacity,
        codec_executor=codec,
    )
    task = asyncio.create_task(runtime.realize_geometry(_request(_mapped_binding(tmp_path))))
    try:
        await _wait_for_supervisor_entry_or_task(task, codec_supervisor.entered)

        assert product_supervisor.calls == 2  # numeric precheck then instantiation
        assert product_supervisor.scratch is not None and product_supervisor.scratch.exists()
        assert codec_supervisor.scratch is not None and codec_supervisor.scratch.exists()
        assert capacity.locked()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not product_supervisor.scratch.exists()
        assert not codec_supervisor.scratch.exists()
        assert not capacity.locked()
        await asyncio.wait_for(capacity.acquire(), timeout=0.2)
        host_descriptor = await capacity.acquire_host(time.monotonic() + 0.2)
        os.close(host_descriptor)
        capacity.release()
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await runtime.close()


@pytest.mark.asyncio
async def test_preview_launches_once_and_rejects_unverifiable_geometry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _TrackingSupervisor()
    runtime = OcpCadQueryProductRealizationRuntime(
        capacity_root=tmp_path / "capacity",
        supervisor=supervisor,  # type: ignore[arg-type]
    )

    async def reject_geometry(requests):
        assert len(requests) == 2  # world plus exact local instance
        raise ValueError("simulated corrupt BREP")

    monkeypatch.setattr(runtime._codec, "normalize_batch", reject_geometry)
    with pytest.raises(ProductRealizationRuntimeValidationError) as raised:
        await runtime.realize_geometry(_request(_mapped_binding(tmp_path)))

    assert raised.value.code == "REALIZATION_OUTPUT_INVALID"
    assert raised.value.phase == "output_validation"
    assert supervisor.calls == 2
    assert supervisor.scratch is not None and not supervisor.scratch.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected"),
    (
        (
            "OCCT BREP geometry is invalid",
            "geometry is invalid",
        ),
        ("private/path/and-sensitive-value", "output failed geometry verification"),
    ),
)
async def test_preview_codec_validation_preserves_diagnostic_and_releases_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, expected: str
) -> None:
    supervisor = _TrackingSupervisor()
    runtime = OcpCadQueryProductRealizationRuntime(
        capacity_root=tmp_path / "capacity", supervisor=supervisor
    )

    async def reject_geometry(requests):
        assert len(requests) == 2
        raise GeometryRuntimeValidationError(message)

    monkeypatch.setattr(runtime._codec, "normalize_batch", reject_geometry)
    try:
        with pytest.raises(ProductRealizationRuntimeValidationError) as raised:
            await runtime.realize_geometry(_request(_mapped_binding(tmp_path)))
        assert raised.value.code == "REALIZATION_OUTPUT_INVALID"
        assert raised.value.phase == "output_validation"
        assert raised.value.diagnostic["reason"] == message
        from CAD2BIM_backend.application.product_realization_preview_service import _runtime_validation_message
        public_message = _runtime_validation_message(raised.value)
        assert expected in public_message
        assert "private/path" not in public_message
        assert not isinstance(raised.value, ProductRealizationRuntimeUnavailableError)
        assert supervisor.calls == 2
        assert supervisor.scratch is not None and not supervisor.scratch.exists()
        await asyncio.wait_for(runtime._capacity.acquire(), timeout=0.2)
        descriptor = await runtime._capacity.acquire_host(time.monotonic() + 0.2)
        os.close(descriptor)
        runtime._capacity.release()
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("worker_timeout", "worker_rss", "codec"))
async def test_started_product_or_codec_limit_preserves_nonretry_subtype_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    supervisor = _TrackingSupervisor()
    runtime = OcpCadQueryProductRealizationRuntime(
        capacity_root=tmp_path / "capacity", supervisor=supervisor
    )
    codec_calls = []

    async def fail_worker(spec, *, budget, deadline, interaction, on_guard_registered=None):
        del budget, deadline, interaction, on_guard_registered
        supervisor.calls += 1
        supervisor.scratch = spec.scratch
        raise WorkerSupervisorError(
            "WORKER_TIMEOUT" if stage == "worker_timeout" else "WORKER_RESOURCE_LIMIT",
            "private native limit detail",
            execution_started=True,
        )

    async def fail_codec(requests):
        codec_calls.append(len(requests))
        # The codec continues under Product's existing admitted execution lease.
        from CAD2BIM_backend.infrastructure.worker_runtime.capacity import current_execution_lease

        lease = current_execution_lease()
        assert lease is not None and lease.descriptor is not None
        assert runtime._capacity.locked()
        raise GeometryRuntimeResourceLimitError("private native limit detail")

    if stage == "codec":
        monkeypatch.setattr(runtime._codec, "normalize_batch", fail_codec)
    else:
        monkeypatch.setattr(supervisor, "run", fail_worker)
    with pytest.raises(ProductRealizationRuntimeResourceLimitError) as raised:
        await runtime.realize_geometry(_request(_mapped_binding(tmp_path)))
    from CAD2BIM_backend.application.product_realization_preview_service import _runtime_resource_limit_message
    assert "private native limit detail" not in _runtime_resource_limit_message(raised.value)
    assert raised.value.retryable is False
    assert supervisor.calls == (2 if stage == "codec" else 1)
    assert codec_calls == ([2] if stage == "codec" else [])
    assert supervisor.scratch is not None and not supervisor.scratch.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("family, case_key", [
    ("anode", "a1_horizontal"),
    ("bend", "b1_direct"),
    ("connector", "k1_direct_s_sleeve"),
    ("cross", "c1_four_diameter_groups"),
    ("gas_meter", "m1_middle_port"),
    ("pipe", "self_origin_zero"),
    ("regulator", "g2_shifted_finite_window"),
    ("riser", "r1_short"),
    ("service_saddle", "s1_orthogonal"),
    ("tee", "t1_known_straight"),
    ("transition", "x1_direct_six_to_one"),
    ("valve", "v2_offset_reducer"),
])
async def test_current_system_family_representative_self_geometry(tmp_path, family, case_key):
    """One unchanged current case per Family through supervised builds and finalization."""
    from tests.unit.scenarios.family_numeric_fixtures import current_numeric_request

    root = Path(__file__).resolve().parents[2] / "scenarios/engineering_product_families"
    contract = ProductFamilyContract.model_validate_json(
        (root / f"families/{family}/definition.json").read_text())
    cases = json.loads((root / f"families/{family}/preview_cases.json").read_text())["cases"]["items"]
    case = next(item for item in cases if item["case_key"] == case_key)
    assert case["invocation"]["realization_kind"] == "self_geometry"
    assert case["expectation"]["expected_status"] == "SOLVED"
    invocation = case["invocation"]
    context = SimpleNamespace(
        parameters=lambda: invocation["public_parameters"],
        product_origin=lambda: invocation["product_origin"],
        external_constraints=lambda: invocation.get("external_constraints", ()),
    )
    request = current_numeric_request(root, family, contract, context)
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    try:
        prechecks = [result async for result in runtime.precheck_batch((request,))]
        executions = [execution async for execution in runtime.instantiate_batch(
            (request,), prechecks, output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS)]
    finally:
        await runtime.close()
    execution, = executions
    result = execution.realization
    assert result.proposal.status is ProductRealizationStatus.SOLVED
    assert result.frame_check_status == "PASS", result.model_dump(mode="json")
    assert len(result.external_condition_checks) == len(request.invocation.external_constraints)
    assert all(check.evaluation.satisfied for check in result.external_condition_checks)
    expected_fragments = {fragment.fragment_key for fragment in contract.geometry_fragments}
    assert expected_fragments
    assert {receipt.fragment_key for receipt in result.instance_receipts} == expected_fragments
    assert set(execution.instance_geometries) == expected_fragments
    assert all(geometry.payload for geometry in execution.instance_geometries.values())
    assert execution.geometry is None
