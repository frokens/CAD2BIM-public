"""Independent numeric interface execution and its existing sample consumers."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from CAD2BIM_backend.application.geometry_resource_service import build_geometry_function_sample, GeometryResourceBuildError
from CAD2BIM_backend.application.product_realization_preview_service import ProductRealizationPreviewService
from CAD2BIM_backend.domain.geometry_runtime import GeometryValidateFunctionRequest, GeometryBuildInput, GeometryRuntimeValidationError
from CAD2BIM_backend.domain.preview_cases import FamilyPreviewCase, FamilyCaseExpectation, PreviewSourceSnapshot
from CAD2BIM_backend.domain.product_realization import ProductRealizationExternalConstraint
from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.infrastructure.geometry_runtime.adapter import OcpCadQueryGeometryRuntime
from CAD2BIM_backend.infrastructure.product_realization_runtime.adapter import OcpCadQueryProductRealizationRuntime
from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle
from CAD2BIM_mcp.tool_docs.ifc_geometry import (
    GEOMETRY_MODELING_FUNCTION_SOURCE_EXAMPLE, GEOMETRY_INTERFACE_SOURCE_EXAMPLE,
    GEOMETRY_MODELING_FUNCTION_PARAMETER_SCHEMA_EXAMPLE, GEOMETRY_MODELING_FUNCTION_VALIDATION_SAMPLE_EXAMPLE,
)


def test_geometry_interface_entry_in_fresh_process_never_imports_native_stack():
    root = Path(__file__).resolve().parents[3] / "src"
    script = (
        "import sys, io; sys.path.insert(0, " + repr(str(root)) + "); "
        "from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle; "
        "from CAD2BIM_backend.infrastructure.geometry_runtime.build_child import _execute_interfaces; "
        "source = pack_source_bundle({'main.py': b'import cadquery as cq\\ndef build(ctx):\\n    return cq.Workplane().box(1,1,1)\\n', "
        "'interfaces.py': b'def evaluate(ctx):\\n    return None\\n'}); "
        "_execute_interfaces({'mode':'interfaces','sample_parameters':[{}]}, source, io.BytesIO()); "
        "assert not any(n == 'cadquery' or n == 'OCP' or n.startswith('OCP.') for n in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_geometry_numeric_failure_has_no_brep_input_or_native_build(tmp_path, monkeypatch):
    source = pack_source_bundle({"main.py": b"import cadquery as cq\ndef build(ctx):\n    return cq.Workplane().box(1,1,1)\n",
        "interfaces.py": b"def evaluate(ctx):\n    raise ValueError('interface_fault')\n"})
    payload = b"not-a-native-brep"
    request = GeometryValidateFunctionRequest(source_bundle=source,
        parameter_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        sample_parameters=({},), geometry_inputs=(GeometryBuildInput(input_key="body", payload=payload,
            sha256=hashlib.sha256(payload).hexdigest()),))
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    modes = []
    original = runtime._supervisor.run
    async def observed(spec, **kwargs):
        document = json.loads((spec.scratch / "request.json").read_text())
        modes.append(document["mode"])
        assert document["mode"] == "interfaces"
        assert not tuple(spec.scratch.glob("*.brep")) and "geometry_inputs" not in document
        return await original(spec, **kwargs)
    monkeypatch.setattr(runtime._supervisor, "run", observed)
    with pytest.raises(GeometryRuntimeValidationError, match="INTERFACE_EVALUATION_FAILED"):
        await runtime.validate_function(request)
    assert modes == ["interfaces"]


@pytest.mark.asyncio
async def test_geometry_sample_uses_independent_numeric_then_shape_worker(tmp_path, monkeypatch):
    source = pack_source_bundle({"main.py": GEOMETRY_MODELING_FUNCTION_SOURCE_EXAMPLE.encode(),
        "interfaces.py": GEOMETRY_INTERFACE_SOURCE_EXAMPLE.encode()})
    request = GeometryValidateFunctionRequest(source_bundle=source,
        parameter_schema=GEOMETRY_MODELING_FUNCTION_PARAMETER_SCHEMA_EXAMPLE,
        sample_parameters=(GEOMETRY_MODELING_FUNCTION_VALIDATION_SAMPLE_EXAMPLE,))
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    modes = []
    original = runtime._supervisor.run
    async def observed(spec, **kwargs):
        modes.append(json.loads((spec.scratch / "request.json").read_text())["mode"])
        return await original(spec, **kwargs)
    monkeypatch.setattr(runtime._supervisor, "run", observed)
    result = await runtime.validate_function(request)
    assert modes == ["interfaces", "function"]
    assert {item.interface_key for item in result.sample_executions[0].interfaces} == {"inlet", "outlet"}
    assert result.sample_geometry.summary.topology.solid_count == 1


@pytest.mark.asyncio
async def test_geometry_shape_failure_after_successful_interface_check_stays_technical(tmp_path, monkeypatch):
    source = pack_source_bundle({"main.py": b"def build(ctx):\n    raise ValueError('shape_fault')\n",
        "interfaces.py": b"def evaluate(ctx):\n    return None\n"})
    request = GeometryValidateFunctionRequest(source_bundle=source,
        parameter_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        sample_parameters=({},))
    runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "capacity")
    modes = []
    original = runtime._supervisor.run
    async def observed(spec, **kwargs):
        modes.append(json.loads((spec.scratch / "request.json").read_text())["mode"])
        return await original(spec, **kwargs)
    monkeypatch.setattr(runtime._supervisor, "run", observed)
    with pytest.raises(GeometryRuntimeValidationError, match="BUILD_EXECUTION_FAILED"):
        await runtime.validate_function(request)
    assert modes == ["interfaces", "function"]


@pytest.mark.asyncio
async def test_query_sample_interface_error_precedes_exact_heavy_input_read():
    from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole
    resources = SimpleNamespace(read_exact_asset=AsyncMock(), load_build_inputs=AsyncMock())
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    def asset_pair(identity, **kwargs):
        if kwargs["asset_role"] is GeometryResourceAssetRole.SOURCE_BUNDLE:
            return SimpleNamespace(asset_format="source_bundle_v1", content_type="application/zip", sha256="a" * 64), b"source"
        return SimpleNamespace(asset_format="json_schema", content_type="application/schema+json", sha256="b" * 64), json.dumps(schema).encode()
    resources.read_exact_asset.side_effect = asset_pair
    runtime = SimpleNamespace(evaluate_function_interfaces=AsyncMock(side_effect=GeometryRuntimeValidationError("interface_fault")),
        instantiate_function=AsyncMock())
    bundle = SimpleNamespace(resource=SimpleNamespace(resource_id="geometry"),
        version=SimpleNamespace(resource_version_id="exact", manifest={}))
    with pytest.raises(GeometryResourceBuildError, match="interface_fault"):
        await build_geometry_function_sample(resources, runtime, SimpleNamespace(identity=object()), bundle, {})
    resources.load_build_inputs.assert_not_awaited()
    runtime.instantiate_function.assert_not_awaited()


@pytest.mark.asyncio
async def test_family_preview_normal_fail_skips_materials_and_next_case_builds(tmp_path):
    from tests.unit.backend.test_product_realization_runtime import _request, _mapped_binding
    from tests.unit.backend.product_family_fixtures import current_family_contract
    actual = _mapped_binding(tmp_path)
    family = current_family_contract(interfaces=({"interface_key": "port", "owner_component_key": "body", "interface_kind": "external",
        "source": {"source_kind": "model_interface", "fragment_key": "body_shape", "model_interface_key": "datum"},
        "external_constraint": {"allowed_kinds": ["fixed_point"], "allowed_directions": ["none"],
            "maximum_position_tolerance_m": 1e-6, "maximum_angular_tolerance_rad": 1e-6}},))
    base = _request(actual).model_copy(update={"family_contract": family, "family_contract_digest": sha256_digest(family),
        "geometry_bindings": (actual.model_copy(update={"geometry": None}),)})
    def at(point):
        condition = ProductRealizationExternalConstraint.model_validate({"interface_key": "port", "source_kind": "design_target",
            "condition": {"kind": "fixed_point", "point_m": point, "direction_relation": "none"}})
        return base.model_copy(update={"invocation": base.invocation.model_copy(update={"external_constraints": (condition,)})})
    requests = (at([0.1, 0, 0]), at([0, 0, 0]))
    material_reads = []
    async def material_loader(requests, checks):
        material_reads.append(checks[0].frame_check_status)
        return tuple(request.model_copy(update={"geometry_bindings": (actual,)}) for request in requests)
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "capacity")
    service = ProductRealizationPreviewService(None, None, runtime, None)
    cases = tuple(FamilyPreviewCase(case_key=key, invocation=request.invocation,
        expectation=FamilyCaseExpectation(expected_status="SOLVED")) for key, request in zip(("fails", "passes"), requests))
    source = PreviewSourceSnapshot(source_kind="family_realization", source_type="published", source_id="family", source_ref="exact",
        snapshot_id="exact-version", content_digest="a" * 64, title="Two-phase Preview")
    result = await service.preview_cases(SimpleNamespace(), source=source, requests=requests, cases=cases, material_loader=material_loader)
    assert [item.execution_status for item in result.report.cases] == ["completed", "completed"]
    assert [item.realization.execution_stage for item in result.report.cases] == ["interface_constraints_evaluated", "instantiated"]
    assert [item.expectation_check for item in result.report.cases] == ["fail", "pass"]
    assert material_reads == ["PASS"]
