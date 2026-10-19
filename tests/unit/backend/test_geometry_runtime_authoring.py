from __future__ import annotations

import hashlib
import base64
import asyncio
import os
import json
import math
from pathlib import Path
from types import SimpleNamespace

import cadquery as cq
import pytest

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.geometry_interface import (
    MAX_GEOMETRY_MODEL_INTERFACES_BYTES,
    GeometryModelInterface,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryBuildFixedRequest,
    GeometryBuildInput,
    GeometryRuntimeValidationError,
    GeometrySummary,
    GeometryValidateFunctionRequest,
    normalize_geometry_function_parameters,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.adapter import (
    OcpCadQueryGeometryRuntime,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import (
    serialize_user_shape,
)
from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle


_CANONICAL_REOPEN_FIXTURES = (
    Path(__file__).resolve().parents[2] / "fixtures" / "geometry_runtime" / "canonical_reopen"
)
_ATTEMPT_04_FIXTURE = _CANONICAL_REOPEN_FIXTURES / "attempt_04"


def _bundle(files: dict[str, str]) -> bytes:
    return pack_source_bundle({path: source.encode("utf-8") for path, source in files.items()})

def _function_bundle(files: dict[str, str]) -> bytes:
    """Current Shape-only fixture with an explicit empty numerical interface entry."""
    return _bundle({"interfaces.py": "def evaluate(ctx):\n    pass\n", **files})


def _named_brep_input() -> GeometryBuildInput:
    payload = serialize_user_shape(cq.Solid.makeBox(1, 1, 1))
    return GeometryBuildInput(input_key="base", payload=payload, sha256=hashlib.sha256(payload).hexdigest())


@pytest.mark.parametrize("compiler_version", ["8.5", "8.4", None])
def test_controlled_environment_checks_supported_compiler(monkeypatch, compiler_version):
    from CAD2BIM_backend.infrastructure.geometry_runtime import modeling_environment
    from CAD2BIM_backend.infrastructure.product_realization_runtime.child import _ChildError, _require_modeling_environment

    def version(name):
        assert name == "RestrictedPython"
        if compiler_version is None:
            raise modeling_environment.importlib.metadata.PackageNotFoundError(name)
        return compiler_version

    monkeypatch.setattr(modeling_environment.importlib.metadata, "version", version)
    if compiler_version == "8.5":
        _require_modeling_environment()
    else:
        with pytest.raises(_ChildError, match="RestrictedPython 8.5") as failure:
            _require_modeling_environment()
        assert failure.value.code == "REALIZATION_RUNTIME_UNAVAILABLE"
        assert failure.value.phase == "preflight"


@pytest.mark.asyncio
async def test_named_brep_input_channel_is_reclaimed_on_cancellation(tmp_path):
    from CAD2BIM_backend.infrastructure.worker_runtime import WorkerProcessSupervisor
    started = asyncio.Event()
    launches = []
    class WaitingSupervisor:
        async def run(self, launch, **kwargs):
            launches.append(launch)
            started.set()
            await asyncio.Future()
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity", supervisor=WaitingSupervisor())
    request = GeometryBuildFixedRequest(source_bundle=_bundle({"main.py":"def build(ctx): return ctx.geometry('base')"}),
        geometry_inputs=(_named_brep_input(),))
    task = asyncio.create_task(runtime.build_fixed(request))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        launch = launches[0]
        input_fd = int(launch.env["CAD2BIM_GEOMETRY_BUILD_INPUT_FDS"])
        assert os.fstat(input_fd).st_size == len(request.geometry_inputs[0].payload)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not launch.scratch.exists()
        for descriptor in launch.pass_fds:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        runtime._supervisor = WorkerProcessSupervisor()
        result = await runtime.build_fixed(request)
        assert result.geometry.summary.volume_m3 == pytest.approx(1)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_named_brep_inputs_are_editable_independent_copies_in_real_build(tmp_path: Path) -> None:
    source = _named_brep_input()
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        result = await runtime.build_fixed(GeometryBuildFixedRequest(
            source_bundle=_bundle({"main.py": """import cadquery as cq
def build(ctx):
    first = ctx.geometry('base')
    first = first.translate((4, 0, 0))
    second = ctx.geometry('base')
    trimmed = second.cut(cq.Solid.makeBox(0.5, 1, 1))
    return cq.Compound.makeCompound([first, trimmed])
"""}), geometry_inputs=(source,),
        ))
        assert result.geometry.summary.volume_m3 == pytest.approx(1.5)
        assert result.geometry.summary.bounds.minimum_m[0] == pytest.approx(0.5, abs=1e-6)
        assert result.geometry.summary.bounds.maximum_m[0] == pytest.approx(5, abs=1e-6)
        assert result.geometry.summary.topology.solid_count == 2
        assert hashlib.sha256(source.payload).hexdigest() == source.sha256
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_named_brep_function_inputs_survive_new_samples_and_runtime_restart(tmp_path: Path) -> None:
    source = _named_brep_input()
    bundle = _bundle({'main.py': "def build(ctx):\n    return ctx.geometry('base').translate((ctx.param('dx'), 0, 0))\n",
'interfaces.py': 'def evaluate(ctx):\n    pass\n'})
    schema = {"type": "object", "additionalProperties": False, "required": ["dx"],
        "properties": {"dx": {"type": "number", "minimum": 0, "maximum": 10}}}
    for samples in (({"dx": 0}, {"dx": 2}), ({"dx": 4},)):
        runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
        try:
            result = await runtime.validate_function(GeometryValidateFunctionRequest(source_bundle=bundle,
                parameter_schema=schema, sample_parameters=samples, geometry_inputs=(source,)))
            assert [item.geometry_summary.bounds.minimum_m[0] for item in result.sample_executions] == pytest.approx([sample["dx"] for sample in samples], abs=1e-6)
            assert all(item.geometry_summary.volume_m3 == pytest.approx(1) for item in result.sample_executions)
        finally:
            await runtime.close()


@pytest.mark.asyncio
async def test_named_brep_input_unknown_key_has_actionable_worker_diagnostic(tmp_path: Path) -> None:
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(GeometryRuntimeValidationError, match="missing.*not declared"):
            await runtime.build_fixed(GeometryBuildFixedRequest(
                source_bundle=_bundle({"main.py": "def build(ctx):\n    return ctx.geometry('missing')\n"}),
                geometry_inputs=(_named_brep_input(),),
            ))
    finally:
        await runtime.close()


def test_named_brep_input_limits_and_digest_are_checked_before_worker() -> None:
    source = _named_brep_input()
    with pytest.raises(ValueError, match="digest mismatch"):
        GeometryBuildInput(input_key="base", payload=source.payload, sha256="0" * 64)
    with pytest.raises(ValueError, match="unique"):
        GeometryBuildFixedRequest(source_bundle=b"x", geometry_inputs=(source, source))
    with pytest.raises(ValueError, match="at most 8"):
        GeometryBuildFixedRequest(source_bundle=b"x", geometry_inputs=tuple(
            source.model_copy(update={"input_key": f"part_{i}"}) for i in range(9)))
    payload = b"x" * (16 * 1024 * 1024)
    inputs = tuple(GeometryBuildInput(input_key=f"part_{i}", payload=payload,
        sha256=hashlib.sha256(payload).hexdigest()) for i in range(3))
    with pytest.raises(ValueError, match="aggregate 32 MiB"):
        GeometryBuildFixedRequest(source_bundle=b"x", geometry_inputs=inputs)


def _path_parameter_schema():
    return {
        "type": "object", "additionalProperties": False, "required": ["points"],
        "properties": {
            "points": {"type": "array", "minItems": 2, "maxItems": 128,
                       "items": {"type": "array", "minItems": 3, "maxItems": 3,
                                 "items": {"type": "number", "minimum": -100, "maximum": 100}}},
            "section": {"type": "object", "additionalProperties": False, "required": [],
                        "default": {}, "properties": {
                            "diameter": {"type": "number", "default": 0.1, "exclusiveMinimum": 0},
                        }},
        },
    }


def test_structured_geometry_parameters_normalize_defaults_and_own_their_values():
    from CAD2BIM_backend.infrastructure.geometry_runtime.build_context import GeometryBuildContext

    values = {"points": [[0, 0, 0], [1, 0, 0], [1, 2, 0]]}
    normalized = normalize_geometry_function_parameters(_path_parameter_schema(), values)
    assert normalized["section"] == {"diameter": 0.1}
    assert "section" not in values
    context = GeometryBuildContext(normalized)
    context.param("points")[1][0] = 999
    assert context.param("points")[1][0] == normalized["points"][1][0] == 1
    normalized["points"][1][0] = 10
    assert values["points"][1][0] == 1
    assert context.param("points")[1][0] == 1


def test_prepared_geometry_rules_validate_once_and_isolate_nested_defaults(monkeypatch):
    from CAD2BIM_backend.domain import geometry_runtime as domain

    schema = _path_parameter_schema()
    original = domain._safe_parameter_schema_validator
    calls = []
    monkeypatch.setattr(domain, "_safe_parameter_schema_validator",
                        lambda value: (calls.append(1), original(value))[1])
    prepared = domain._PreparedGeometryFunctionParameters(schema)
    values = {"points": [[0, 0, 0], [1, 0, 0]]}
    first = prepared.normalize_for(schema, values)
    first["section"]["diameter"] = 99
    first["points"][1][0] = 99
    assert prepared.normalize_for(schema, values) == {
        **values, "section": {"diameter": 0.1},
    }
    for invalid in ({}, {"points": [[0, 0, 0], [True, 0, 0]]},
                    {**values, "section": {"diameter": -1}}):
        with pytest.raises(ValueError):
            prepared.normalize_for(schema, invalid)
    assert len(calls) == 1
    schema["properties"]["section"]["properties"]["diameter"]["default"] = 0.2
    with pytest.raises(ValueError, match="exact binding"):
        prepared.normalize_for(schema, values)
    assert prepared.normalize(values)["section"]["diameter"] == 0.1
    assert domain._PreparedGeometryFunctionParameters(schema).normalize(values)["section"]["diameter"] == 0.2
    # Python dict equality treats True == 1; exact Schema identity must not.
    scalar_schema = {"type": "object", "additionalProperties": False, "required": [],
                     "properties": {"n": {"type": "number", "minimum": 1}}}
    scalar_rules = domain._PreparedGeometryFunctionParameters(scalar_schema)
    scalar_schema["properties"]["n"]["minimum"] = True
    with pytest.raises(ValueError, match="exact binding"):
        scalar_rules.normalize_for(scalar_schema, {"n": 2})


def test_function_registration_samples_share_rules_without_sharing_defaults(monkeypatch):
    from CAD2BIM_backend.domain import geometry_runtime as domain

    calls = []
    original = domain._safe_parameter_schema_validator
    monkeypatch.setattr(domain, "_safe_parameter_schema_validator",
                        lambda schema: (calls.append(1), original(schema))[1])
    request = domain.GeometryValidateFunctionRequest(
        source_bundle=b"bounded-test-source", parameter_schema=_path_parameter_schema(),
        sample_parameters=({"points": [[0, 0, 0], [1, 0, 0]]},) * 3,
    )
    assert len(calls) == 1
    request.sample_parameters[0]["section"]["diameter"] = 99
    assert request.sample_parameters[1]["section"]["diameter"] == 0.1


@pytest.mark.parametrize("points", [
    [[0, 0, 0]], [[0, 0, 0], [1, 2]], [[0, 0, 0], [True, 2, 3]],
    [[0, 0, 0], ["1", 2, 3]], [[0, 0, 0], [float("nan"), 2, 3]],
    [[0, 0, 0], [101, 2, 3]], [[0, 0, 0]] * 129,
])
def test_structured_geometry_parameters_reject_invalid_points(points):
    with pytest.raises(ValueError):
        normalize_geometry_function_parameters(_path_parameter_schema(), {"points": points})


@pytest.mark.parametrize("change", [
    {"maxItems": 129}, {"items": {"$ref": "#"}}, {"items": True},
    {"minItems": False}, {"maxItems": None}, {"additionalProperties": True},
])
def test_structured_geometry_schema_requires_closed_bounded_nodes(change):
    schema = _path_parameter_schema()
    schema["properties"]["points"].update(change)
    with pytest.raises(ValueError):
        normalize_geometry_function_parameters(schema, {"points": [[0, 0, 0], [1, 0, 0]]})


def _tagged_shape_schema():
    return {"type": "object", "additionalProperties": False, "required": ["shape"],
            "properties": {"shape": {"anyOf": [
                {"type": "object", "additionalProperties": False, "required": ["mode", field],
                 "properties": {"mode": {"type": "string", "enum": [mode]},
                                field: {"type": "number", "minimum": 0.001},
                                "tolerance": {"type": "number", "default": 0.01}}}
                for mode, field in (("curved", "radius_m"), ("sphere", "diameter_m"))]}}}


def test_geometry_tagged_object_normalizes_only_selected_branch_without_mutating_input():
    values = {"shape": {"mode": "sphere", "diameter_m": 0.5}}
    normalized = normalize_geometry_function_parameters(_tagged_shape_schema(), values)
    assert normalized == {"shape": {"mode": "sphere", "diameter_m": 0.5, "tolerance": 0.01}}
    assert "tolerance" not in values["shape"]
    assert "radius_m" not in normalized["shape"]


@pytest.mark.parametrize("value", [
    {}, {"mode": "unknown"}, {"mode": "sphere"},
    {"mode": "sphere", "radius_m": 1},
    {"mode": "sphere", "diameter_m": 1, "radius_m": 1},
])
def test_geometry_tagged_object_rejects_missing_or_mixed_branch_values(value):
    with pytest.raises(ValueError, match="schema-valid"):
        normalize_geometry_function_parameters(_tagged_shape_schema(), {"shape": value})


@pytest.mark.parametrize("change", ["duplicate", "optional_tag", "default_tag", "multi_choice", "open", "keyword"])
def test_geometry_tagged_object_rejects_general_union_schemas(change):
    schema = _tagged_shape_schema()
    branch = schema["properties"]["shape"]["anyOf"][1]
    tag = branch["properties"]["mode"]
    if change == "duplicate":
        tag["enum"] = ["curved"]
    elif change == "optional_tag":
        branch["required"].remove("mode")
    elif change == "default_tag":
        tag["default"] = "sphere"
    elif change == "multi_choice":
        tag["enum"] = ["sphere", "other"]
    elif change == "open":
        branch["additionalProperties"] = True
    else:
        schema["properties"]["shape"]["if"] = {}
    with pytest.raises(ValueError):
        normalize_geometry_function_parameters(schema, {"shape": {"mode": "sphere", "diameter_m": 1}})


def test_structured_geometry_parameters_accept_array_boundary_and_reject_unknown_fields():
    schema = _path_parameter_schema()
    points = [[index / 2, 0, 0] for index in range(128)]
    assert len(normalize_geometry_function_parameters(schema, {"points": points})["points"]) == 128
    with pytest.raises(ValueError, match="section"):
        normalize_geometry_function_parameters(schema, {"points": points, "section": {"unknown": 1}})


def test_parameter_audit_geometry_schema_exact_depth_boundary():
    leaf = {"type": "number"}
    for _ in range(3):
        leaf = {"type": "array", "minItems": 0, "maxItems": 1, "items": leaf}
    schema = {"type": "object", "additionalProperties": False, "required": [],
              "properties": {"path": leaf}}
    # Root is depth zero; three arrays put the scalar at exactly depth four.
    assert normalize_geometry_function_parameters(schema, {}) == {}
    leaf["items"] = {"type": "array", "minItems": 0, "maxItems": 1, "items": leaf["items"]}
    with pytest.raises(ValueError, match="depth 4"):
        normalize_geometry_function_parameters(schema, {})


def test_parameter_audit_geometry_schema_exact_node_boundary():
    schema = {"type": "object", "additionalProperties": False, "required": [], "properties": {}}
    # Root + four object nodes + 251 scalar nodes = 256 nodes; each object stays <=64 fields.
    for group, count in enumerate((64, 64, 64, 59)):
        schema["properties"][f"group_{group}"] = {
            "type": "object", "additionalProperties": False, "required": [],
            "properties": {f"field_{i}": {"type": "number"} for i in range(count)},
        }
    assert normalize_geometry_function_parameters(schema, {}) == {}
    schema["properties"]["group_3"]["properties"]["extra"] = {"type": "number"}
    with pytest.raises(ValueError, match="256 nodes"):
        normalize_geometry_function_parameters(schema, {})


@pytest.mark.parametrize("target", ["schema", "input"])
def test_parameter_audit_geometry_utf8_byte_boundary(target):
    schema = {"type": "object", "additionalProperties": False, "required": [],
              "properties": {"text": {"type": "string"}}, "description": ""}
    values = {"text": ""}
    document, key = (schema, "description") if target == "schema" else (values, "text")
    remaining = 16_384 - len(canonical_json(document).encode("utf-8"))
    document[key] = "é" * (remaining // 2) + "x" * (remaining % 2)
    assert len(canonical_json(document).encode("utf-8")) == 16_384
    assert normalize_geometry_function_parameters(schema, values) == values
    document[key] += "x"
    with pytest.raises(ValueError, match="16 KiB|16384"):
        normalize_geometry_function_parameters(schema, values)


def test_parameter_audit_geometry_nested_defaults_normalize_without_aliasing():
    schema = {"type": "object", "additionalProperties": False, "required": ["sections"],
              "properties": {"sections": {"type": "array", "minItems": 1, "maxItems": 128,
                  "items": {"type": "object", "additionalProperties": False, "required": [],
                      "properties": {"width": {"type": "number", "minimum": 0, "default": 0.5}}}}}}
    implicit = {"sections": [{}, {}]}
    explicit = {"sections": [{"width": 0.5}, {"width": 0.5}]}
    result = normalize_geometry_function_parameters(schema, implicit)
    assert result == normalize_geometry_function_parameters(schema, explicit)
    assert normalize_geometry_function_parameters(schema, result) == result
    result["sections"][0]["width"] = 1
    assert implicit == {"sections": [{}, {}]}
    assert result["sections"][1]["width"] == 0.5
    schema["properties"]["sections"]["items"]["properties"]["width"] = {"type": "string", "default": "x" * 128}
    with pytest.raises(ValueError, match="16 KiB|16384"):
        normalize_geometry_function_parameters(schema, {"sections": [{} for _ in range(128)]})


@pytest.mark.parametrize("child", [
    {"type": "number", "minimum": 2, "default": 1},
    {"type": "object", "additionalProperties": False, "properties": {}, "required": [], "default": {"extra": 1}},
    {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "number"}, "default": []},
])
def test_parameter_audit_geometry_rejects_invalid_nested_defaults_even_when_not_used(child):
    schema = {"type": "object", "additionalProperties": False, "required": [], "properties": {"unused": child}}
    with pytest.raises(ValueError, match="default violates"):
        normalize_geometry_function_parameters(schema, {})


def test_parameter_audit_geometry_each_object_has_its_own_64_field_limit():
    properties = {f"value_{index}": {"type": "number"} for index in range(64)}
    schema = {"type": "object", "additionalProperties": False, "required": [], "properties": properties}
    assert normalize_geometry_function_parameters(schema, {}) == {}
    properties["extra"] = {"type": "number"}
    with pytest.raises(ValueError, match="properties are invalid"):
        normalize_geometry_function_parameters(schema, {})


def _multipoint_pipe_request(points, *, section=None):
    root = Path(__file__).resolve().parents[2] / "fixtures/geometry_runtime/multipoint_pipe"
    return GeometryValidateFunctionRequest(
        source_bundle=_bundle({p.name: p.read_text() for p in root.glob("*.py")}),
        parameter_schema=json.loads((root / "parameter_schema.json").read_text()),
        sample_parameters=({"points_m": points, "outer_diameter_m": 0.1,
                            "section": section or {"mode": "wall_thickness", "wall_thickness_m": 0.005}},),
    )


def _assert_pipe_end_face(shape, point, outward_axis, *, inner_diameter=0.09):
    # This inspects actual planar annular faces, not a source/receipt coordinate copy.
    end_faces = [face for face in shape.Faces()
                 if face.geomType() == "PLANE" and (face.Center() - cq.Vector(*point)).Length < 1e-7]
    assert len(end_faces) == 1
    face = end_faces[0]
    assert face.normalAt().dot(cq.Vector(*outward_axis)) > 1 - 1e-9
    assert face.Area() == pytest.approx(math.pi * (0.1**2 - inner_diameter**2) / 4, abs=1e-10)
    assert len(face.Wires()) == (2 if inner_diameter else 1)


@pytest.mark.asyncio
async def test_multipoint_pipe_build_reopens_with_actual_end_faces_and_normalized_defaults(tmp_path):
    from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import reopen_user_shape

    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        result = await runtime.validate_function(_multipoint_pipe_request([[0, 0, 0], [1, 0, 0], [1, 2, 0]]))
        shape = reopen_user_shape(result.sample_geometry.payload)
        assert shape.isValid() and len(shape.Solids()) == 1
        assert shape.Volume() == pytest.approx(3 * math.pi * (0.1**2 - 0.09**2) / 4, rel=1e-6)
        _assert_pipe_end_face(shape, (0, 0, 0), (-1, 0, 0))
        _assert_pipe_end_face(shape, (1, 2, 0), (0, 1, 0))
        interfaces = {item.interface_key: item for item in result.sample_executions[0].interfaces}
        assert interfaces["inlet"].local_frame.translation_m == (0, 0, 0)
        assert interfaces["outlet"].local_frame.translation_m == (1, 2, 0)
        assert interfaces["datum"].profile is None
        with pytest.raises(AssertionError):
            _assert_pipe_end_face(shape, (1, 2.01, 0), (0, 1, 0))
        with pytest.raises(AssertionError):
            _assert_pipe_end_face(shape, (1, 2, 0), (0, -1, 0))
        envelope_request = _multipoint_pipe_request([[0, 0, 0], [1, 0, 0]], section={"mode": "solid_envelope"})
        envelope = await runtime.validate_function(envelope_request)
        assert envelope_request.sample_parameters[0]["section"]["wall_thickness_m"] == 0
        _assert_pipe_end_face(reopen_user_shape(envelope.sample_geometry.payload), (1, 0, 0), (1, 0, 0), inner_diameter=0)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_build_fixed_uses_main_entry_and_relative_memory_imports(
    tmp_path: Path,
) -> None:
    source_bundle = _bundle(
        {
            "main.py": (
                "from .shapes import make_shape\n\n"
                "def build(ctx):\n"
                "    return make_shape(ctx.param('length'))\n"
            ),
            "shapes.py": (
                "import cadquery as cq\n\n"
                "def make_shape(length):\n"
                "    return cq.Workplane('XY').box(length, 2.0, 3.0)\n"
            ),
        }
    )
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        result = await runtime.build_fixed(
            GeometryBuildFixedRequest(
                source_bundle=source_bundle,
                build_parameters={"length": 4.0},
            )
        )
    finally:
        await runtime.close()

    assert result.geometry.format is CanonicalGeometryFormat.OCCT_BREP_V1
    assert result.geometry.summary.bounds.minimum_m == (-2.0, -1.0, -1.5)
    assert result.geometry.summary.bounds.maximum_m == (2.0, 1.0, 1.5)
    assert result.interfaces == ()
    assert "modeling_execution_contract" not in type(result).model_fields
    assert result.geometry.summary.volume_m3 == 24.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "return_expression",
    (
        "cq.Workplane('XY').box(1, 2, 3).val()",
        "cq.Workplane('XY').box(1, 2, 3)",
    ),
)
async def test_build_fixed_accepts_controlled_shape_or_workplane(
    tmp_path: Path,
    return_expression: str,
) -> None:
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        result = await runtime.build_fixed(
            GeometryBuildFixedRequest(
                source_bundle=_bundle(
                    {
                        "main.py": (
                            "import cadquery as cq\n\n"
                            "def build(ctx):\n"
                            f"    return {return_expression}\n"
                        )
                    }
                )
            )
        )
    finally:
        await runtime.close()

    assert result.geometry.summary.topology.solid_count == 1
    assert result.geometry.summary.volume_m3 == 6.0


@pytest.mark.asyncio
async def test_build_fixed_rejects_non_unique_workplane_and_blocked_import(
    tmp_path: Path,
) -> None:
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(GeometryRuntimeValidationError, match="exactly one shape"):
            await runtime.build_fixed(
                GeometryBuildFixedRequest(
                    source_bundle=_bundle(
                        {
                            "main.py": (
                                "import cadquery as cq\n\n"
                                "def build(ctx):\n"
                                "    return cq.Workplane('XY').newObject([cq.Solid.makeBox(1, 1, 1), cq.Solid.makeBox(1, 1, 1, cq.Vector(2, 0, 0))])\n"
                            )
                        }
                    )
                )
            )
        with pytest.raises(GeometryRuntimeValidationError, match="BUILD_SOURCE_INVALID.*only imports declared"):
            await runtime.build_fixed(
                GeometryBuildFixedRequest(
                    source_bundle=_bundle(
                        {"main.py": ("import os\n\ndef build(ctx):\n    return os.getcwd()\n")}
                    )
                )
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_private_bundle_absolute_import_has_location_and_relative_recovery(tmp_path):
    source = {
        "main.py": "from helpers import make_shape\n\ndef build(ctx): return make_shape()\n",
        "helpers.py": "import cadquery as cq\n\ndef make_shape(): return cq.Solid.makeBox(1, 2, 3)\n",
    }
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(GeometryRuntimeValidationError) as error:
            await runtime.build_fixed(GeometryBuildFixedRequest(source_bundle=_bundle(source)))
        message = str(error.value)
        assert "BUILD_SOURCE_INVALID" in message
        assert '"exception_type":"ModelingCapabilityError"' in message
        assert '"source_path":"main.py"' in message
        assert '"line":1' in message
        assert "use a relative helper or declared capability" in message
        assert '"source_line":"from helpers import make_shape"' in message
        assert "/Users/" not in message and "Traceback" not in message
        source["main.py"] = source["main.py"].replace("from helpers", "from .helpers")
        result = await runtime.build_fixed(GeometryBuildFixedRequest(source_bundle=_bundle(source)))
        assert result.geometry.summary.volume_m3 == pytest.approx(6)
    finally:
        await runtime.close()


def _controlled_program(source, helpers=None):
    from CAD2BIM_backend.infrastructure.worker_runtime.python_bundle import compile_python_bundle
    return compile_python_bundle(
        _bundle({"main.py": source, **(helpers or {})}), package_name="controlled_test",
        entry_name="build", allowed_import_roots={"math", "cadquery"},
        profile_label="Geometry", maximum_members=16, maximum_member_bytes=65_536,
        maximum_total_bytes=262_144, controlled_modeling=True,
    )


@pytest.mark.parametrize("helper", [
    "import OCP\n", "from cadquery.occ_impl import shapes\n",
    "def helper(x=print('loaded')): return x\n",
    "@print\ndef helper(): return 1\n",
    "def helper(x: print('loaded')): return x\n",
    "def helper(_getattr_, /): return 1\n",
    "def helper():\n    class Native: pass\n",
    "def helper():\n    import cadquery as cq\n    cq.Workplane.box = None\n",
    "def helper(): return ().__class__\n",
])
def test_controlled_modeling_checks_every_helper_before_module_load(helper):
    from CAD2BIM_backend.infrastructure.worker_runtime.python_bundle import PythonBundleValidationError
    with pytest.raises(PythonBundleValidationError) as error:
        _controlled_program("def build(ctx): return None\n", {"helpers.py": helper})
    assert error.value.diagnostic["source_path"] == "helpers.py"


def test_controlled_modeling_fresh_defaults_helpers_and_synchronous_callbacks():
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    program = _controlled_program(
        "from .helpers import _size\nimport cadquery as cq\n"
        "def build(ctx):\n    sizes=[3,1,2]\n"
        "    n=min(sizes,key=lambda v:v*v)\n"
        "    return cq.Workplane('XY').box(_size(n),2,3)\n",
        {"helpers.py": "def _size(x, seen=[]):\n    seen.append(x)\n    return len(seen)\n"},
    )
    for _ in range(2):
        shape, _ = execute_geometry_build(program, {})
        assert shape.Volume() == pytest.approx(6)


def test_controlled_construction_observes_workplane_location_and_all_compound_operands():
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import reopen_user_shape

    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n"
        "    plane=cq.Plane(origin=(7,3,-2),normal=(0,0,1))\n"
        "    box=cq.Workplane(plane).box(2,4,6).val()\n"
        "    cutter=cq.Solid.makeCylinder(.3,8,cq.Vector(7,3,-6),cq.Vector(0,0,1))\n"
        "    left=box.cut(cutter)\n"
        "    right=cq.Solid.makeSphere(.5,cq.Vector(11,3,-2),angleDegrees1=-90,angleDegrees2=90)\n"
        "    return cq.Compound.makeCompound([left,right])\n"
    )
    shape, _, construction = execute_geometry_build(program, {}, capture_construction=True)
    nodes = {node.id: node for node in construction.nodes}
    root = nodes[construction.root]
    assert root.op == "Compound.makeCompound"
    assert len(root.children) == 2
    assert {nodes[child].op for child in root.children} == {"cut", "Solid.makeSphere"}
    cut = next(nodes[child] for child in root.children if nodes[child].op == "cut")
    assert len(cut.children) == 2
    box = next(node for node in construction.nodes if node.op == "box")
    observed_box = reopen_user_shape(base64.b64decode(box.shape_brep))
    assert observed_box.BoundingBox().xmin == pytest.approx(6)
    assert observed_box.BoundingBox().ymin == pytest.approx(1)
    assert observed_box.BoundingBox().zmin == pytest.approx(-5)
    assert len(shape.Solids()) == 2
    # The compound is encoded from its two operands; its final canonical BREP
    # belongs to the independent Codec path, not a duplicate trace snapshot.
    assert root.shape_brep is None
    assert cut.shape_brep is None


def test_controlled_construction_detaches_pending_profile_before_extrude_clears_it():
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import reopen_user_shape

    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n"
        "    return cq.Workplane(cq.Plane(origin=(3,4,5),normal=(0,0,1))).circle(2).extrude(7).val()\n"
    )
    shape, _, construction = execute_geometry_build(program, {}, capture_construction=True)
    nodes = {node.id: node for node in construction.nodes}
    extrude = next(node for node in construction.nodes if node.op == "extrude")
    state = extrude.kwargs["$receiver_state"]
    assert state["plane"]["$plane"]["origin"] == [3.0, 4.0, 5.0]
    assert len(state["pending_wires"]) == 1
    wire = state["pending_wires"][0]
    assert wire["$shape_type"] == "Wire"
    observed_wire = reopen_user_shape(base64.b64decode(wire["$shape_brep"]))
    assert observed_wire.BoundingBox().xmin == pytest.approx(1)
    assert nodes[extrude.receiver].kwargs["$observed_plane"]["$plane"]["origin"] == [3.0, 4.0, 5.0]
    assert shape.Volume() == pytest.approx(math.pi * 4 * 7)
    assert extrude.shape_brep is None
    assert nodes[extrude.receiver].shape_brep is None
    assert nodes[construction.root].shape_brep is None


def test_controlled_construction_tracks_computed_vector_receivers_without_numeric_trace_payload():
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build

    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n"
        "    origin=cq.Vector(0,0,0)\n"
        "    axis=(cq.Vector(0,0,2)-origin).normalized()\n"
        "    start=(origin+axis).toTuple()\n"
        "    return cq.Solid.makeCylinder(1,2,cq.Vector(start[0],start[1],start[2]),axis)\n"
    )
    shape, _, construction = execute_geometry_build(program, {}, capture_construction=True)
    assert shape.BoundingBox().zmin == pytest.approx(1)
    assert [node.op for node in construction.nodes] == ["Solid.makeCylinder"]
    assert construction.nodes[0].args[2] == {"$vector": [0.0, 0.0, 1.0]}
    assert construction.nodes[0].args[3] == {"$vector": [0.0, 0.0, 1.0]}


@pytest.mark.parametrize("expression, start", [
    ("cq.Plane(origin=(3,4,5), normal=(0,0,1)).xDir", [2., 0., 0.]),
    ("cq.Plane(origin=(3,4,5), normal=(0,0,1)).yDir", [0., 2., 0.]),
    ("cq.Plane(origin=(3,4,5), normal=(0,0,1)).zDir", [0., 0., 2.]),
    ("cq.Plane(origin=(3,4,5), normal=(0,0,1)).origin", [6., 8., 10.]),
    ("cq.Solid.makeBox(2,4,6).BoundingBox().center", [2., 4., 6.]),
])
def test_controlled_construction_tracks_property_vectors_into_real_shape(expression, start):
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build

    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n"
        f"    direction={expression}\n"
        "    start=(direction*2).toTuple()\n"
        "    return cq.Solid.makeCylinder(.25,2,cq.Vector(start[0],start[1],start[2]),cq.Vector(0,0,1))\n"
    )
    shape, _, construction = execute_geometry_build(program, {}, capture_construction=True)
    assert shape.isValid() and shape.Volume() == pytest.approx(math.pi * .25**2 * 2)
    assert shape.Center().toTuple() == pytest.approx((start[0], start[1], start[2]+1))
    assert [node.op for node in construction.nodes] == ["Solid.makeCylinder"]
    assert construction.nodes[0].args[2] == {"$vector": start}


@pytest.mark.parametrize("capture", [False, True])
def test_controlled_modeling_native_property_failure_is_sticky(monkeypatch, capture):
    from CAD2BIM_backend.infrastructure.geometry_runtime import modeling_capabilities
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingCapabilityError
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build

    modeling_capabilities._initialize_native()
    def fail(value):
        raise RuntimeError("injected native property failure")
    monkeypatch.setitem(modeling_capabilities._PROPERTIES[cq.Plane], "xDir", fail)
    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n"
        "    try:\n        direction=cq.Plane(origin=(0,0,0),normal=(0,0,1)).xDir\n"
        "    except:\n        pass\n"
        "    return cq.Solid.makeBox(1,1,1)\n"
    )
    with pytest.raises(ModelingCapabilityError, match="injected native property failure"):
        execute_geometry_build(program, {}, capture_construction=capture)


def test_construction_private_json_requires_finite_values_and_real_utf8_roundtrip():
    from pydantic import ValidationError
    from pydantic_core import PydanticSerializationError
    from CAD2BIM_backend.domain.ifc_geometry_construction import GeometryConstruction

    def construction(value):
        return GeometryConstruction(root=0, nodes=({"id": 0, "op": "observed", "args": [value]},))

    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            construction({"nested": [value]})
    # A private Python/JSON observation has no JavaScript-safe-integer contract.
    original = construction({"nested": [2**53, "中文", 1.25]})
    assert GeometryConstruction.model_validate_json(original.model_dump_json().encode("utf-8")) == original
    for value in ({"nested": ["\ud800"]}, {"\ud800": 1}):
        with pytest.raises(PydanticSerializationError, match="surrogates not allowed"):
            construction(value).model_dump_json().encode("utf-8")
    for text in ('"\\ud800"', '"\\udc00"'):
        payload = ('{"root":0,"nodes":[{"id":0,"op":"observed","args":[' + text + ']}]}').encode("ascii")
        with pytest.raises(ValidationError, match="Invalid JSON"):
            GeometryConstruction.model_validate_json(payload)
    invalid_utf8 = b'{"root":0,"nodes":[{"id":0,"op":"observed","args":["\xed\xa0\x80"]}]}'
    with pytest.raises(ValidationError, match="Invalid JSON"):
        GeometryConstruction.model_validate_json(invalid_utf8)


@pytest.mark.parametrize("nodes,message", (
    (({"id": 0, "op": "observed"}, {"id": 0, "op": "observed"}), "unique nodes"),
    (({"id": 0, "op": "observed", "args": [{"$ref": 1}]},), "missing node"),
    (({"id": 0, "op": "observed", "children": [1]},
      {"id": 1, "op": "observed", "receiver": 0}), "acyclic"),
))
def test_construction_private_json_still_requires_a_closed_acyclic_graph(nodes, message):
    from pydantic import ValidationError
    from CAD2BIM_backend.domain.ifc_geometry_construction import GeometryConstruction

    with pytest.raises(ValidationError, match=message):
        GeometryConstruction(root=0, nodes=nodes)


def test_family_numeric_profile_allows_math_and_rejects_direct_or_helper_modeling():
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_family_program
    from CAD2BIM_backend.infrastructure.worker_runtime.python_bundle import (
        PythonBundleValidationError,
        compile_python_bundle,
    )

    class Host:
        def param(self, key):
            return {"diameter": 2.0}[key]

        contract = parameters = external_constraints = policy = lambda self: {}
        product_origin = lambda self: (1.0, 2.0, 3.0)

    def compile_family(sources):
        return compile_python_bundle(
            _bundle(sources), package_name="controlled_numeric_family",
            entry_name="realize", allowed_import_roots={"math"},
            profile_label="Product realization", maximum_members=16,
            maximum_member_bytes=65_536, maximum_total_bytes=262_144,
            reject_private_attributes=True, controlled_modeling=True,
            controlled_import_roots={"math"},
        )

    program = compile_family({
        "main.py": "from .solver import solve\ndef realize(ctx): return solve(ctx)\n",
        "solver.py": "import math\ndef solve(ctx): return {'status':'SOLVED','value':math.sqrt(ctx.param('diameter') ** 2),'roundoff':math.ulp(ctx.param('diameter')),'origin':ctx.product_origin()}\n",
    })
    assert execute_family_program(program, Host()) == {
        "status": "SOLVED", "value": 2.0, "roundoff": math.ulp(2.0), "origin": (1.0, 2.0, 3.0),
    }
    for sources in (
        {"main.py": "import cadquery as cq\ndef realize(ctx): return {}\n"},
        {"main.py": "from .shape import solve\ndef realize(ctx): return solve(ctx)\n",
         "shape.py": "import cadquery as cq\ndef solve(ctx): return cq.Solid.makeBox(1,1,1)\n"},
    ):
        with pytest.raises(PythonBundleValidationError):
            compile_family(sources)


@pytest.mark.parametrize("failure", [None, "load", "context", "entry"])
def test_family_numeric_entry_timing_only_covers_author_call(monkeypatch, failure):
    from contextlib import contextmanager
    from CAD2BIM_backend.infrastructure.geometry_runtime import modeling_execution

    events = []

    class Arena:
        def __init__(self, *, profile):
            assert profile == "family_numeric"

        def import_module(self, *args, **kwargs):
            pass

        def globals(self):
            return {}

        def author_context(self, host, *, family):
            events.append("context")
            if failure == "context":
                raise ValueError("context rejected")
            assert family and host == "host"
            return self

        def ensure_active(self):
            events.append("ensure")

        def snapshot(self, result):
            events.append("snapshot")
            return result

        def close(self):
            events.append("close")

    class Program:
        def load_entry(self, *args, **kwargs):
            events.append("load")
            if failure == "load":
                raise ValueError("load rejected")

            def entry(context):
                events.append("entry")
                if failure == "entry":
                    raise ValueError("author rejected")
                return {"status": "INFEASIBLE"}

            return entry

    @contextmanager
    def phase(name):
        assert name == "family_numeric_entry"
        events.append("timer_start")
        try:
            yield
        finally:
            events.append("timer_end")

    monkeypatch.setattr(modeling_execution, "ModelingArena", Arena)
    monkeypatch.setattr(modeling_execution, "timed_phase", phase)
    if failure is None:
        assert modeling_execution.execute_family_program(Program(), "host") == {"status": "INFEASIBLE"}
        assert events == ["load", "context", "timer_start", "entry", "timer_end", "ensure", "snapshot", "close"]
    else:
        with pytest.raises(ValueError):
            modeling_execution.execute_family_program(Program(), "host")
        expected = ["load"]
        if failure != "load":
            expected.append("context")
        if failure == "entry":
            expected.extend(("timer_start", "entry", "timer_end"))
        assert events == [*expected, "close"]


@pytest.mark.parametrize("statement", [
    "shape.wrapped", "cq.Workplane.box", "ctx.interfaces()",
    "shape.missing_api()", "format_value = f'{shape}'",
    "str(shape)", "format_value = f'{shape!r}'",
])
def test_controlled_modeling_boundary_failure_cannot_be_swallowed(statement):
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingCapabilityError
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n"
        "    shape=cq.Workplane('XY').box(1,2,3).val()\n"
        f"    try:\n        {statement}\n    except:\n        pass\n    return shape\n"
    )
    with pytest.raises(ModelingCapabilityError):
        execute_geometry_build(program, {})


def test_controlled_modeling_arena_rejects_cross_call_and_expired_handles():
    import cadquery as cq
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingArena, ModelingCapabilityError
    first, second = ModelingArena(), ModelingArena()
    handle = first.wrap(cq.Vector(1, 2, 3))
    with pytest.raises(ModelingCapabilityError, match="cross-invocation"):
        second.unwrap(handle)
    first.close()
    with pytest.raises(ModelingCapabilityError, match="expired"):
        first.unwrap(handle)


def test_controlled_modeling_wire_revolve_and_sticky_native_failure(monkeypatch):
    import math
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingCapabilityError
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n"
        "    wire=cq.Wire.makePolygon([(1,0,0),(2,0,0),(2,0,2),(1,0,2)],close=True)\n"
        "    try:\n        return cq.Solid.revolve(wire,[],360,(0,0,0),(0,0,1))\n"
        "    except:\n        return cq.Solid.makeBox(1,1,1)\n"
    )
    shape, _, construction = execute_geometry_build(program, {}, capture_construction=True)
    assert type(shape) is cq.Solid and shape.isValid()
    assert shape.Volume() == pytest.approx(6 * math.pi)
    nodes = {node.id: node for node in construction.nodes}
    revolve = nodes[construction.root]
    wire = nodes[revolve.args[0]["$ref"]]
    assert wire.shape_brep is not None
    assert revolve.shape_brep is None
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("injected revolve failure")
    monkeypatch.setattr(cq.Solid, "revolve", fail)
    with pytest.raises(ModelingCapabilityError, match="revolve"):
        execute_geometry_build(program, {})
    assert calls == [True]


@pytest.mark.parametrize("invalid", ["face", "inner", "angle", "bool", "axis", "degenerate", "cross", "expired"])
def test_controlled_modeling_wire_revolve_rejects_before_native(monkeypatch, invalid):
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingArena, ModelingCapabilityError
    arena, other = ModelingArena(), ModelingArena()
    wire = cq.Wire.makePolygon([(1,0,0),(2,0,0),(2,0,2),(1,0,2)], close=True)
    args = [arena.wrap(wire), [], 360, (0,0,0), (0,0,1)]
    if invalid == "face": args[0] = arena.wrap(cq.Face.makeFromWires(wire))
    if invalid == "inner": args[1] = [arena.wrap(cq.Vector())]
    if invalid == "angle": args[2] = float("nan")
    if invalid == "bool": args[2] = True
    if invalid == "axis": args[4] = (0,0,float("inf"))
    if invalid == "degenerate": args[4] = (0,0,0)
    if invalid in {"cross", "expired"}:
        args[0] = other.wrap(wire)
        if invalid == "expired": other.close()
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("malformed revolve entered native")
    monkeypatch.setattr(cq.Solid, "revolve", forbidden)
    constructor = arena.getattr(arena.getattr(arena.import_module("cadquery"), "Solid"), "revolve")
    try:
        with pytest.raises(ModelingCapabilityError): constructor(*args)
        assert calls == []
    finally:
        arena.close()
        other.close()


@pytest.mark.parametrize("retention", ["global", "local_cycle"])
def test_controlled_modeling_close_revokes_host_and_instance_references_without_gc(tmp_path, retention):
    import gc
    import weakref
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingArena, ModelingCapabilityError
    from CAD2BIM_backend.infrastructure.geometry_runtime.build_context import GeometryBuildContext
    native_refs = []
    class ObservedContext(GeometryBuildContext):
        def geometry(self, key):
            shape = super().geometry(key)
            native_refs.append(weakref.ref(shape))
            return shape
    template = cq.Solid.makeBox(0.5, 0.5, 0.5)
    host = ObservedContext({}, {"body_geometry": template})
    host_ref = weakref.ref(host)
    retained = ("    KEEP.extend([ctx, shape, ctx.param, ctx.geometry])\n" if retention == "global"
                else "    loop=[]\n    loop.extend([loop, ctx, shape, ctx.param, ctx.geometry])\n")
    program = _controlled_program("KEEP=[]\ndef build(ctx):\n    shape=ctx.geometry('body_geometry')\n"
                                  + retained + "    return shape\n")
    arena = ModelingArena()
    entry = program.load_entry(arena.import_module, execution_globals=arena.globals())
    view = arena.author_context(host)
    expired = arena.getattr(view, 'geometry')
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        authored = entry(view)
        assert native_refs[0]() is not None
        arena.close()
        assert authored is not None
        del host
        del template
        assert host_ref() is None
        assert native_refs[0]() is None
        with pytest.raises(ModelingCapabilityError, match="expired"):
            expired('body_geometry')
    finally:
        arena.close()
        if was_enabled:
            gc.enable()


def test_controlled_topology_enumeration_preserves_native_order_identity_and_orientation():
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingArena

    solid = cq.Solid.makeBox(1, 2, 3)
    reversed_solid = cq.Solid(solid.wrapped.Reversed())
    compound = cq.Compound.makeCompound([solid, reversed_solid.translate((3, 0, 0))])
    sphere = cq.Solid.makeSphere(1)
    edge_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(sphere.wrapped, TopAbs_EDGE, edge_map)
    assert any(BRep_Tool.Degenerated_s(TopoDS.Edge_s(edge_map.FindKey(index)))
               for index in range(1, edge_map.Extent() + 1))
    face = solid.Faces()[0]
    edge = face.Edges()[0]
    shapes = (solid, cq.Shape(solid.wrapped), reversed_solid, compound,
              cq.Compound.makeCompound([]), sphere, face, edge)
    arena = ModelingArena()
    try:
        for shape in shapes:
            handle = arena.wrap(shape)
            for name in ("Faces", "Edges", "Wires", "Solids", "Shells", "Vertices"):
                expected = getattr(shape, name)()
                actual = arena.unwrap(arena.getattr(handle, name)())
                assert type(actual) is list and len(actual) == len(expected)
                assert all(type(got) is type(want)
                           and got.wrapped.IsEqual(want.wrapped)
                           and got.wrapped.Orientation() == want.wrapped.Orientation()
                           for got, want in zip(actual, expected, strict=True))
    finally:
        arena.close()


def test_controlled_modeling_retains_real_subtopology_for_fillet_and_face_return():
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n"
        "    shape=cq.Solid.makeBox(2,2,2)\n"
        "    edges=shape.Edges()\n"
        "    rounded=shape.fillet(0.1,edges)\n"
        "    return max(rounded.Faces(),key=lambda f:f.Area())\n"
    )
    shape, _, construction = execute_geometry_build(program, {}, capture_construction=True)
    assert shape.isValid() and shape.ShapeType() == "Face"
    root = next(node for node in construction.nodes if node.id == construction.root)
    assert root.op == "Faces"
    assert any(node.op == "Edges" for node in construction.nodes)


def test_controlled_modeling_rejects_edges_from_a_different_native_parent():
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingCapabilityError
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n"
        "    a=cq.Solid.makeBox(1,1,1)\n    b=cq.Solid.makeBox(2,2,2)\n"
        "    return a.fillet(0.1,b.Edges())\n"
    )
    with pytest.raises(ModelingCapabilityError, match="parent shape"):
        execute_geometry_build(program, {})


@pytest.mark.parametrize("operation", ["fillet", "chamfer"])
def test_controlled_workplane_rejects_foreign_edges_before_native_builder(monkeypatch, operation):
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingCapabilityError
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n"
        "    a=cq.Workplane().box(1,1,1)\n    b=cq.Solid.makeBox(2,2,2)\n"
        f"    return a.newObject(b.Edges()).{operation}(0.1)\n"
    )
    native_calls = []
    def forbidden(*args, **kwargs):
        native_calls.append(operation)
        raise AssertionError("foreign topology reached native builder")
    monkeypatch.setattr(cq.Solid, operation, forbidden)
    monkeypatch.setattr(cq.Compound, operation, forbidden)
    with pytest.raises(ModelingCapabilityError, match="parent shape"):
        execute_geometry_build(program, {})
    assert native_calls == []


@pytest.mark.parametrize("selection,operation,size", [
    ("a.edges()", "fillet", 1),
    ("a.faces('+Z').edges()", "chamfer", 1),
    ("a.newObject([b]).edges()", "fillet", 2),
])
def test_controlled_workplane_preserves_actual_target_selection(selection, operation, size):
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n"
        "    a=cq.Workplane().box(1,1,1)\n    b=cq.Solid.makeBox(2,2,2)\n"
        f"    return {selection}.{operation}(0.1)\n"
    )
    shape, _ = execute_geometry_build(program, {})
    assert shape.isValid() and 0 < shape.Volume() < size ** 3
    assert shape.BoundingBox().xlen == pytest.approx(size)


def test_controlled_workplane_precheck_failure_cannot_be_swallowed(monkeypatch):
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingCapabilityError
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n"
        "    a=cq.Workplane().box(1,1,1).edges()\n"
        "    try:\n        a.fillet(0.1)\n    except:\n        pass\n"
        "    return cq.Solid.makeBox(1,1,1)\n"
    )
    original = cq.Workplane.findSolid
    failures = []
    def fail_selected(self, *args, **kwargs):
        if self.objects and all(type(item) is cq.Edge for item in self.objects):
            failures.append(True)
            raise RuntimeError("injected Workplane target lookup failure")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(cq.Workplane, "findSolid", fail_selected)
    with pytest.raises(ModelingCapabilityError, match="injected Workplane target lookup failure"):
        execute_geometry_build(program, {})
    assert failures == [True]


def test_family_numeric_undeclared_capability_failure_cannot_be_swallowed():
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingCapabilityError
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_family_program
    from CAD2BIM_backend.infrastructure.worker_runtime.python_bundle import compile_python_bundle
    class Host:
        def param(self, key): return None
        contract = parameters = product_origin = external_constraints = policy = lambda self: {}
    program = compile_python_bundle(_bundle({"main.py":
        "def realize(ctx):\n    try:\n        ctx.instantiate('body')\n"
        "    except:\n        pass\n    return {'status':'INFEASIBLE'}\n"}),
        package_name="controlled_family_test", entry_name="realize",
        allowed_import_roots={"math"}, profile_label="Family",
        maximum_members=16, maximum_member_bytes=65_536, maximum_total_bytes=262_144,
        controlled_modeling=True, controlled_import_roots={"math"})
    host = Host()
    with pytest.raises(ModelingCapabilityError, match="instantiate"):
        execute_family_program(program, host)


def test_controlled_modeling_accepts_inert_export_metadata():
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    program = _controlled_program(
        "from .helpers import make\ndef build(ctx): return make()\n",
        {"helpers.py": "import cadquery as cq\n__all__=['make']\n"
         "def make(): return cq.Solid.makeBox(1,2,3)\n"},
    )
    shape, _ = execute_geometry_build(program, {})
    assert shape.Volume() == pytest.approx(6)


@pytest.mark.parametrize("operation", ["input_copy", "topology_check", "topology_iteration", "vector_iteration"])
def test_controlled_modeling_native_preparation_failure_is_sticky(monkeypatch, operation):
    from CAD2BIM_backend.infrastructure.geometry_runtime import modeling_capabilities
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingCapabilityError
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    shape = cq.Solid.makeBox(1, 1, 1)
    expression = {"input_copy": "ctx.geometry('base')", "topology_check": "shape.fillet(0.1, [])",
                  "topology_iteration": "shape.Faces()",
                  "vector_iteration": "[value for value in cq.Vector(1,2,3)]"}[operation]
    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n    shape=cq.Solid.makeBox(1,1,1)\n"
        f"    try:\n        {expression}\n    except:\n        pass\n    return shape\n"
    )
    def fail(*args, **kwargs):
        raise RuntimeError("injected native preparation failure")
    owner, name = {"input_copy": (cq.Shape, "copy"), "topology_check": (cq.Shape, "Edges"),
                   "topology_iteration": (modeling_capabilities, "_indexed_shape_entities"),
                   "vector_iteration": (cq.Vector, "toTuple")}[operation]
    monkeypatch.setattr(owner, name, fail)
    with pytest.raises(ModelingCapabilityError, match="injected native preparation failure"):
        execute_geometry_build(program, {}, {"base": shape})


def test_controlled_modeling_missing_named_input_remains_pre_native_key_error():
    from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import execute_geometry_build
    program = _controlled_program(
        "import cadquery as cq\ndef build(ctx):\n    try:\n        ctx.geometry('missing')\n"
        "    except KeyError:\n        return cq.Solid.makeBox(1,2,3)\n"
    )
    shape, _ = execute_geometry_build(program, {})
    assert shape.Volume() == pytest.approx(6)


def test_import_policy_diagnostic_keeps_nested_member_and_single_file_profile_boundaries():
    from CAD2BIM_backend.infrastructure.worker_runtime.python_bundle import (
        PythonBundleValidationError, compile_python_bundle,
    )
    from CAD2BIM_backend.infrastructure.worker_runtime.python_user_code import (
        PythonUserCodeValidationError, compile_user_module,
    )
    with pytest.raises(PythonBundleValidationError) as error:
        compile_python_bundle(
            _bundle({"main.py": "def build(ctx): return None\n", "parts/shape.py": "import os\n"}),
            package_name="import_diagnostic_test", entry_name="build",
            allowed_import_roots={"math", "cadquery", "OCP"}, profile_label="Geometry authoring",
            maximum_members=16, maximum_member_bytes=65_536, maximum_total_bytes=262_144,
        )
    assert error.value.diagnostic["source_path"] == "parts/shape.py"
    assert error.value.diagnostic["line"] == 1
    assert "unavailable module: os" in error.value.diagnostic["message"]
    with pytest.raises(PythonUserCodeValidationError) as error:
        compile_user_module(
            "from .helpers import make_shape\n\ndef query(ctx): return None\n",
            filename="<cad2bim-query>",
            required_entry_name="query", allowed_import_roots={"math"},
            profile_label="Geometry query", allow_relative_imports=False,
        )
    assert error.value.diagnostic["exception_type"] == "ImportPolicyError"
    assert "relative imports are unavailable" in error.value.diagnostic["message"]
    assert "for example" not in error.value.diagnostic["message"]


@pytest.mark.asyncio
async def test_build_failure_reports_only_bounded_bundle_member_diagnostic(
    tmp_path: Path,
) -> None:
    source_bundle = _bundle(
        {
            "main.py": (
                "from .shape import make_shape\n\ndef build(ctx):\n    return make_shape()\n"
            ),
            "shape.py": (
                "import cadquery as cq\n\n"
                "def make_shape():\n"
                "    return cq.Workplane('XY').box(1, 1, 1).missing_api()\n"
            ),
        }
    )
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(GeometryRuntimeValidationError) as error:
            await runtime.build_fixed(GeometryBuildFixedRequest(source_bundle=source_bundle))
    finally:
        await runtime.close()

    message = str(error.value)
    assert "BUILD_EXECUTION_FAILED" in message
    assert '"exception_type":"ModelingCapabilityError"' in message
    assert '"source_path":"shape.py"' in message
    assert '"line":4' in message
    assert '"function":"make_shape"' in message
    assert "missing_api" in message
    assert "/Users/" not in message
    assert "Traceback" not in message
    assert "<cad2bim-bundle:" not in message


@pytest.mark.asyncio
async def test_build_syntax_failure_reports_exact_source_member(
    tmp_path: Path,
) -> None:
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(GeometryRuntimeValidationError) as error:
            await runtime.build_fixed(
                GeometryBuildFixedRequest(
                    source_bundle=_bundle(
                        {
                            "main.py": (
                                "from .shape import make_shape\n\n"
                                "def build(ctx):\n"
                                "    return make_shape()\n"
                            ),
                            "shape.py": "def make_shape(:\n    return None\n",
                        }
                    )
                )
            )
    finally:
        await runtime.close()

    message = str(error.value)
    assert "BUILD_SOURCE_INVALID" in message
    assert '"exception_type":"SyntaxError"' in message
    assert '"source_path":"shape.py"' in message
    assert '"line":1' in message
    assert "/Users/" not in message
    assert "Traceback" not in message


@pytest.mark.asyncio
async def test_validate_function_requires_schema_valid_samples_and_returns_preview_shape(
    tmp_path: Path,
) -> None:
    source_bundle = _bundle(
        {'main.py': "import cadquery as cq\n\ndef build(ctx):\n    return cq.Workplane('XY').box(ctx.param('length'), 2.0, 3.0)\n",
'interfaces.py': 'def evaluate(ctx):\n    pass\n'}
    )
    schema = {
        "type": "object",
        "properties": {"length": {"type": "number", "minimum": 0.1, "maximum": 10.0}},
        "required": ["length"],
        "additionalProperties": False,
    }
    with pytest.raises(ValueError, match="schema-valid"):
        GeometryValidateFunctionRequest(
            source_bundle=source_bundle,
            parameter_schema=schema,
            sample_parameters=({"length": 0.0},),
        )

    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        result = await runtime.validate_function(
            GeometryValidateFunctionRequest(
                source_bundle=source_bundle,
                parameter_schema=schema,
                sample_parameters=({"length": 1.0}, {"length": 4.0}),
            )
        )
    finally:
        await runtime.close()

    assert len(result.sample_executions) == 2
    assert result.sample_geometry.summary.volume_m3 == 6.0
    assert result.sample_executions[0].geometry_summary == result.sample_geometry.summary
    assert result.sample_executions[1].geometry_summary.volume_m3 == 24.0
    assert all(execution.interfaces == () for execution in result.sample_executions)


@pytest.mark.asyncio
async def test_validate_function_canonicalizes_oblique_boolean_samples_and_replays_exactly(
    tmp_path: Path,
) -> None:
    source = (_ATTEMPT_04_FIXTURE / "main.py").read_bytes()
    request_bytes = (_ATTEMPT_04_FIXTURE / "request.json").read_bytes()
    request_document = json.loads(request_bytes)
    request = GeometryValidateFunctionRequest(
        source_bundle=pack_source_bundle({"main.py": source, "interfaces.py": b"def evaluate(ctx):\n    pass\n"}),
        parameter_schema=request_document["parameter_schema"],
        sample_parameters=tuple(request_document["sample_parameters"]),
    )
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        first = await runtime.validate_function(request)
        replay = await runtime.validate_function(request)
    finally:
        await runtime.close()

    assert len(first.sample_executions) == 2
    assert first.sample_geometry.sha256 == first.sample_executions[0].output_sha256
    assert replay.sample_geometry == first.sample_geometry
    assert replay.sample_executions == first.sample_executions


@pytest.mark.asyncio
async def test_authoring_bundle_accepts_only_bounded_python_source(tmp_path: Path) -> None:
    too_many = {
        "main.py": "def build(ctx):\n    return None\n",
        **{f"helper_{index}.py": "VALUE = 1\n" for index in range(16)},
    }
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(GeometryRuntimeValidationError, match="at most 16"):
            await runtime.build_fixed(GeometryBuildFixedRequest(source_bundle=_bundle(too_many)))
        with pytest.raises(GeometryRuntimeValidationError, match="main.py"):
            await runtime.build_fixed(
                GeometryBuildFixedRequest(
                    source_bundle=_bundle({"other.py": "def build(ctx):\n    return None\n"})
                )
            )
        with pytest.raises(GeometryRuntimeValidationError, match="UTF-8 Python"):
            await runtime.build_fixed(
                GeometryBuildFixedRequest(
                    source_bundle=pack_source_bundle({"main.py": b"\x00\xff"})
                )
            )
        with pytest.raises(GeometryRuntimeValidationError, match="64 KiB each"):
            await runtime.build_fixed(
                GeometryBuildFixedRequest(
                    source_bundle=pack_source_bundle({"main.py": b"#" * (64 * 1024 + 1)})
                )
            )
        with pytest.raises(GeometryRuntimeValidationError, match="256 KiB"):
            await runtime.build_fixed(
                GeometryBuildFixedRequest(
                    source_bundle=pack_source_bundle(
                        {
                            "main.py": b"def build(ctx):\n    return None\n" + b"#" * 60_000,
                            **{
                                f"helper_{index}.py": b"VALUE = 1\n" + b"#" * 60_000
                                for index in range(4)
                            },
                        }
                    )
                )
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_function_samples_keep_geometry_and_independently_evaluated_interfaces(
    tmp_path: Path,
) -> None:
    source_bundle = _bundle(
        {'main.py': "import math\nimport cadquery as cq\n\ndef build(ctx):\n    radius = ctx.param('radius_m')\n    angle_deg = ctx.param('angle_deg')\n    diameter = ctx.param('diameter_m')\n    angle = math.radians(angle_deg)\n    path = (\n        cq.Workplane('XY').moveTo(0.0, 0.0).threePointArc(\n            (radius * math.sin(angle / 2.0), radius * (1.0 - math.cos(angle / 2.0))),\n            (radius * math.sin(angle), radius * (1.0 - math.cos(angle))),\n        )\n    )\n    return cq.Workplane('YZ').circle(diameter / 2.0).sweep(\n        path, isFrenet=True\n    )\n",
'interfaces.py': "import math\n\ndef evaluate(ctx):\n    radius = ctx.param('radius_m')\n    angle_deg = ctx.param('angle_deg')\n    diameter = ctx.param('diameter_m')\n    angle = math.radians(angle_deg)\n    ctx.define_interface(key='inlet', origin_m=(0.0, 0.0, 0.0), axis=(-1.0, 0.0, 0.0), reference_direction=(0.0, 0.0, 1.0), profile={'kind': 'circular', 'diameter_m': diameter})\n    ctx.define_interface(key='outlet', origin_m=(radius * math.sin(angle), radius * (1.0 - math.cos(angle)), 0.0), axis=(math.cos(angle), math.sin(angle), 0.0), reference_direction=(0.0, 0.0, 1.0), profile={'kind': 'circular', 'diameter_m': diameter})\n"}
    )
    schema = {
        "type": "object",
        "properties": {
            "radius_m": {"type": "number", "minimum": 0.1},
            "angle_deg": {"type": "number", "minimum": 1.0, "maximum": 180.0},
            "diameter_m": {"type": "number", "minimum": 0.01},
        },
        "required": ["radius_m", "angle_deg", "diameter_m"],
        "additionalProperties": False,
    }
    samples = (
        {"radius_m": 0.4, "angle_deg": 90.0, "diameter_m": 0.1},
        {"radius_m": 0.8, "angle_deg": 60.0, "diameter_m": 0.2},
    )
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        result = await runtime.validate_function(
            GeometryValidateFunctionRequest(
                source_bundle=source_bundle,
                parameter_schema=schema,
                sample_parameters=samples,
                retain_sample_geometries=True,
            )
        )
    finally:
        await runtime.close()

    def control_shape(sample: dict[str, float]) -> cq.Shape:
        radius = sample["radius_m"]
        angle = math.radians(sample["angle_deg"])
        path = (
            cq.Workplane("XY")
            .moveTo(0.0, 0.0)
            .threePointArc(
                (
                    radius * math.sin(angle / 2.0),
                    radius * (1.0 - math.cos(angle / 2.0)),
                ),
                (
                    radius * math.sin(angle),
                    radius * (1.0 - math.cos(angle)),
                ),
            )
        )
        return (
            cq.Workplane("YZ").circle(sample["diameter_m"] / 2.0).sweep(path, isFrenet=True).val()
        )

    for sample, execution in zip(samples, result.sample_executions, strict=True):
        control = control_shape(sample)
        expected_volume = (
            math.pi
            * (sample["diameter_m"] / 2.0) ** 2
            * sample["radius_m"]
            * math.radians(sample["angle_deg"])
        )
        assert execution.geometry_summary.volume_m3 == pytest.approx(expected_volume, rel=1e-9)
        assert control.Volume() == pytest.approx(expected_volume, rel=1e-9)

        end_faces = [face for face in control.Faces() if face.geomType() == "PLANE"]
        assert len(end_faces) == 2
        for interface in execution.interfaces:
            matching_faces = [
                face
                for face in end_faces
                if face.Center().toTuple()
                == pytest.approx(interface.local_frame.translation_m, abs=1e-9)
            ]
            assert len(matching_faces) == 1
            outward_axis = tuple(row[2] for row in interface.local_frame.rotation)
            assert matching_faces[0].normalAt().toTuple() == pytest.approx(outward_axis, abs=1e-9)
            assert matching_faces[0].Area() == pytest.approx(
                math.pi * (sample["diameter_m"] / 2.0) ** 2,
                rel=1e-9,
            )

    first, second = result.sample_executions
    assert len(result.sample_geometries) == len(result.sample_executions)
    assert result.sample_geometry == result.sample_geometries[0]
    assert tuple(item.sha256 for item in result.sample_geometries) == tuple(item.output_sha256 for item in result.sample_executions)
    assert tuple(item.interface_key for item in first.interfaces) == ("inlet", "outlet")
    assert first.interfaces[1].local_frame.translation_m == pytest.approx((0.4, 0.4, 0.0))
    assert second.interfaces[1].local_frame.translation_m == pytest.approx(
        (0.8 * 3**0.5 / 2.0, 0.4, 0.0)
    )
    assert first.interfaces[1].profile is not None
    assert second.interfaces[1].profile is not None
    assert first.interfaces[1].profile.diameter_m == 0.1
    assert second.interfaces[1].profile.diameter_m == 0.2
    assert first.output_sha256 != second.output_sha256


@pytest.mark.asyncio
async def test_function_samples_retain_large_bounded_interface_collections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    points = tuple(
        (
            round(math.cos(2.0 * math.pi * index / 64), 12),
            round(math.sin(2.0 * math.pi * index / 64), 12),
        )
        for index in range(64)
    )
    raw_interfaces = [
        {
            "interface_key": f"interface_{index:02d}_with_descriptive_name",
            "local_frame": {
                "translation_m": [float(index), 0.0, 0.0],
                "rotation": [
                    [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                    [0.0, 0.0, 1.0],
                ],
            },
            "profile": {"kind": "polygon", "points_m": points},
        }
        for index in range(12)
    ]
    interface_content = canonical_json(raw_interfaces).encode("utf-8")
    assert len(interface_content) < MAX_GEOMETRY_MODEL_INTERFACES_BYTES

    geometry_payload = b"transient-sample-geometry"
    summary = GeometrySummary.model_validate(
        {
            "format": "occt_brep_v1",
            "bounds": {"minimum_m": [0, 0, 0], "maximum_m": [1, 1, 1]},
            "topology": {
                "vertex_count": 8,
                "edge_count": 12,
                "wire_count": 6,
                "face_count": 6,
                "shell_count": 1,
                "solid_count": 1,
                "triangle_count": None,
                "boundary_edge_count": 0,
            },
            "surface_area_m2": 6,
            "volume_m3": 1,
        }
    )
    geometry = CanonicalGeometry(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
        payload=geometry_payload,
        sha256=hashlib.sha256(geometry_payload).hexdigest(),
        summary=summary,
    )
    interfaces = tuple(GeometryModelInterface.model_validate(item) for item in raw_interfaces)
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")

    async def return_output_bundle(**kwargs) -> bytes:
        if kwargs["request_document"]["mode"] == "interfaces":
            return pack_source_bundle({
                f"sample-{index:03d}.interfaces.json": interface_content
                for index in range(len(kwargs["request_document"]["sample_parameters"]))
            })
        return b"worker-output-is-replaced-before-codec-use"

    async def return_normalized_outputs(_bundle, *, expected_count, evaluated_interfaces):
        assert all(sample == interfaces for sample in evaluated_interfaces)
        return tuple(
            SimpleNamespace(geometry=geometry, interfaces=interfaces)
            for _ in range(expected_count)
        )

    monkeypatch.setattr(runtime, "_run_authoring_worker", return_output_bundle)
    monkeypatch.setattr(runtime, "_normalize_authoring_outputs", return_normalized_outputs)
    try:
        result = await runtime.validate_function(
            GeometryValidateFunctionRequest(
                source_bundle=_bundle({'main.py': 'def build(ctx):\n    return None\n',
'interfaces.py': 'def evaluate(ctx):\n    pass\n'}),
                parameter_schema={
                    "type": "object",
                    "properties": {"index": {"type": "integer"}},
                    "required": ["index"],
                    "additionalProperties": False,
                },
                sample_parameters=({"index": 0}, {"index": 1}, {"index": 2}),
            )
        )
        assert len(result.sample_executions) == 3
        assert all(len(execution.interfaces) == len(raw_interfaces)
                   for execution in result.sample_executions)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_build_rejects_duplicate_interface_keys_at_the_source_boundary(
    tmp_path: Path,
) -> None:
    source_bundle = _bundle(
        {
            "main.py": (
                "import cadquery as cq\n\n"
                "def build(ctx):\n"
                "    for _ in range(2):\n"
                "        ctx.define_interface(\n"
                "            key='outlet', origin_m=(0.0, 0.0, 0.0),\n"
                "            axis=(0.0, 0.0, 1.0), "
                "reference_direction=(1.0, 0.0, 0.0),\n"
                "        )\n"
                "    return cq.Workplane('XY').box(1.0, 1.0, 1.0)\n"
            )
        }
    )
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(GeometryRuntimeValidationError) as captured:
            await runtime.build_fixed(GeometryBuildFixedRequest(source_bundle=source_bundle))
    finally:
        await runtime.close()

    assert "BUILD_EXECUTION_FAILED" in str(captured.value)
    assert "defined more than once" in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "axis", "expected_detail"),
    (
        (
            "{'kind': 'circular', 'diameter_m': -0.1}",
            "(0.0, 0.0, 1.0)",
            "greater than 0",
        ),
        (
            "{'kind': 'circular', 'diameter_m': 0.1}",
            "(0.0, 0.0, 2.0)",
            "frame axis must be a unit vector",
        ),
    ),
)
async def test_real_child_rejects_invalid_interface_profile_or_frame(
    tmp_path: Path,
    profile: str,
    axis: str,
    expected_detail: str,
) -> None:
    source_bundle = _bundle(
        {
            "main.py": (
                "import cadquery as cq\n\n"
                "def build(ctx):\n"
                "    ctx.define_interface(\n"
                "        key='outlet', origin_m=(0.0, 0.0, 0.0),\n"
                f"        axis={axis}, reference_direction=(1.0, 0.0, 0.0),\n"
                f"        profile={profile},\n"
                "    )\n"
                "    return cq.Workplane('XY').box(1.0, 1.0, 1.0)\n"
            )
        }
    )
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(GeometryRuntimeValidationError) as captured:
            await runtime.build_fixed(GeometryBuildFixedRequest(source_bundle=source_bundle))
    finally:
        await runtime.close()

    assert "BUILD_EXECUTION_FAILED" in str(captured.value)
    assert expected_detail in str(captured.value)


@pytest.mark.asyncio
async def test_host_rejects_an_unpaired_interface_output_member(tmp_path: Path) -> None:
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(GeometryRuntimeValidationError, match="unexpected sample outputs"):
            await runtime._normalize_authoring_outputs(  # noqa: SLF001
                pack_source_bundle({"sample-000.brep": b"not-opened"}),
                expected_count=1,
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_host_rejects_an_extra_interface_output_member(tmp_path: Path) -> None:
    brep = serialize_user_shape(cq.Workplane("XY").box(1.0, 1.0, 1.0).val())
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(GeometryRuntimeValidationError, match="unexpected sample outputs"):
            await runtime._normalize_authoring_outputs(  # noqa: SLF001
                pack_source_bundle(
                    {
                        "sample-000.brep": brep,
                        "sample-000.interfaces.json": b"[]",
                        "unexpected.json": b"{}",
                    }
                ),
                expected_count=1,
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_host_rejects_noncanonical_interface_output_member(tmp_path: Path) -> None:
    brep = serialize_user_shape(cq.Workplane("XY").box(1.0, 1.0, 1.0).val())
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    try:
        with pytest.raises(
            GeometryRuntimeValidationError,
            match="non-canonical sample interfaces",
        ):
            await runtime._normalize_authoring_outputs(  # noqa: SLF001
                pack_source_bundle(
                    {
                        "sample-000.brep": brep,
                        "sample-000.interfaces.json": b"[ ]",
                    }
                ),
                expected_count=1,
            )
    finally:
        await runtime.close()
