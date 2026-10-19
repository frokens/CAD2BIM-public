"""REG body bindings use the existing mapped-scale and independent sphere owners."""

import hashlib
import json
import math
from pathlib import Path

import pytest

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry, CanonicalGeometryFormat
from CAD2BIM_backend.domain.product_family import ProductFamilyContract
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationRequest, ProductRealizationInvocation,
    ProductRealizationOutputNeed, ResolvedMappedGeometryBinding, ResolvedModelingFunctionBinding,
)
from CAD2BIM_backend.infrastructure.product_realization_runtime import child
from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import reopen_user_shape
from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / "tests/scenarios/engineering_product_families"
FAMILY = BASE / "families/regulator"


def _request(scenario_key):
    manifest = json.loads((FAMILY / "manifest.json").read_text())
    contract = ProductFamilyContract.model_validate_json(
        (FAMILY / "definition.json").read_text())
    source = pack_source_bundle({item["bundle_path"]: (BASE / item["repo_path"]).read_bytes()
                                 for item in manifest["family"]["source_files"]})
    intake = json.loads((BASE / "geometry/regulator_body/asset-intake-plan.json").read_text())
    witness = ROOT / intake["source_path"]
    if not witness.is_file():
        pytest.skip("Exact regulator canonical asset witness is not installed")
    payload = witness.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == intake["source_sha256"]
    fmt = CanonicalGeometryFormat.OCCT_BREP_V1
    bindings = []
    for item in manifest["geometry_bindings"]:
        if item["binding_key"] == "regulator_body_asset":
            bindings.append(ResolvedMappedGeometryBinding(
                binding_key=item["binding_key"], geometry_content_digest=intake["source_sha256"],
                geometry=CanonicalGeometry(format=fmt, content_type=fmt.content_type,
                                           payload=payload, sha256=intake["source_sha256"]),
                interfaces=intake["annotation"]["interfaces"]))
        else:
            bundle = pack_source_bundle({f["bundle_path"]: (BASE / f["repo_path"]).read_bytes()
                                         for f in item["source_files"]})
            digest = hashlib.sha256(bundle).hexdigest()
            bindings.append(ResolvedModelingFunctionBinding(
                binding_key=item["binding_key"], geometry_content_digest=digest,
                source_bundle=bundle, source_bundle_sha256=digest,
                parameter_schema=json.loads((BASE / item["parameter_schema_path"]).read_text())))
    scenario = next(row for row in json.loads((FAMILY / "preview_cases.json").read_text())["cases"]["items"]
                    if row["case_key"] == scenario_key)
    request = ProductRealizationRequest(
        family_contract=contract,
        family_contract_digest=sha256_digest(contract), source_bundle=source,
        source_bundle_sha256=hashlib.sha256(source).hexdigest(), geometry_bindings=tuple(bindings),
        invocation=ProductRealizationInvocation.model_validate(scenario["invocation"]))
    return request, intake, payload


@pytest.mark.parametrize("scenario_key, expected_binding", [
    ("g3_scaled_detailed_after_original_miss", "regulator_body_asset"),
    ("g4_default_nominal_short_window", "sphere_geometry"),
])
def test_regulator_selected_body_build_uses_current_context_and_same_call_interfaces(
        scenario_key, expected_binding):
    request, intake, payload = _request(scenario_key)
    context = child._ProductRealizationContext(request, include_instances=True, include_summary=False)
    program = child._compile_family(request.source_bundle, "regulator_binding_owner", "realize")
    result, _ = child._evaluate_invocation(
        request, program, context, output_need=ProductRealizationOutputNeed.SOLVE_ONLY)
    proposal = result.proposal
    body = next(fragment for fragment in proposal.fragments if fragment.fragment_key == "body_shape")
    assert body.binding_key == expected_binding
    context.build_fragments(proposal)
    receipt = next(item for item in context.receipts if item.fragment_key == "body_shape")
    assert receipt.binding_key == expected_binding
    assert receipt.uniform_scale == body.uniform_scale
    shape = reopen_user_shape(context._instance_payloads["body_shape"])
    assert shape.isValid()
    ports = {item.interface_key: item for item in receipt.interfaces}
    if expected_binding == "regulator_body_asset":
        original = reopen_user_shape(payload)
        scale = body.uniform_scale
        assert .4 <= scale < 1
        assert len(shape.Solids()) == len(original.Solids()) == 13
        assert shape.Volume() == pytest.approx(original.Volume() * scale ** 3, rel=1e-8)
        # Compare geometric coordinates; a tolerance-expanded BREP bounding box
        # is not itself a geometric extent that must scale proportionally.
        expected_vertices = [tuple(v * scale for v in vertex.Center().toTuple())
                             for vertex in original.Vertices()]
        actual_vertices = [vertex.Center().toTuple() for vertex in shape.Vertices()]
        assert len(actual_vertices) == len(expected_vertices)
        for point in expected_vertices:
            assert min(math.dist(point, actual) for actual in actual_vertices) < 1e-12
        for expected in intake["annotation"]["interfaces"]:
            port = ports[expected["interface_key"]]
            assert port.local_frame.translation_m == pytest.approx(
                [v * scale for v in expected["local_frame"]["translation_m"]])
            assert port.local_frame.rotation == tuple(tuple(row) for row in expected["local_frame"]["rotation"])
            assert port.profile.diameter_m == pytest.approx(expected["profile"]["diameter_m"] * scale)
    else:
        parameters = body.geometry_parameters
        assert len(shape.Solids()) == 1
        assert shape.Volume() == pytest.approx(math.pi * parameters["diameter_m"] ** 3 / 6)
        assert receipt.uniform_scale == 1
        for expected in parameters["interfaces"]:
            port = ports[expected["key"]]
            assert list(port.local_frame.translation_m) == expected["origin_m"]
            assert [row[2] for row in port.local_frame.rotation] == expected["axis"]
            assert [row[0] for row in port.local_frame.rotation] == expected["reference_direction"]
            assert port.profile.diameter_m == expected["diameter_m"]
    assert hashlib.sha256(context._instance_payloads["body_shape"]).hexdigest() == receipt.geometry_sha256
