from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.authoring_draft import AuthoringPublicationCommitResult
from CAD2BIM_backend.application.authoring_draft_service import validate_draft_source, AuthoringDraftError
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef,
    CatalogResourceKind,
    CriticalOperationErrorPhase,
    CriticalOperationInitiator,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    DataCatalogEntry,
    AuthoringDraftSelection,
    AuthoringPreviewExampleReference,
    AuthoringPublishOperationDetail,
    PublishedCriticalOperation,
)
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.ifc_geometry_basis import IfcLengthUnit
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyContract,
    ProductFamilyExternalConstraintContract,
    ProductFamilyGeometryPurpose,
    ProductFamilyRecord,
    ProductFamilyVersionAssetRecord,
    ProductFamilyVersionAssetRole,
    normalize_product_family_public_parameters,
)
from CAD2BIM_backend.domain.product_family_authoring import (
    FamilyDraftInlineCreateCommand as FamilyDraftCreateCommand,
    FamilyDraftUpdateCommand,
    FamilyDraftWriteCommand,
    ProductFamilyAuthoringPublication,
    ProductFamilyAuthoringVersionDraft,
    ProductFamilyInvocationSemanticError,
    ResolvedProductFamilyGeometryBindingInput,
    normalize_product_family_validation_scenario,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationStatus,
    ProductRealizationValidationScenario,
    RigidTransform3D,
)
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope, ResourceOwnerScopeKind
from tests.unit.backend.product_family_fixtures import current_family_document


NOW = datetime(2026, 9, 5, tzinfo=UTC)
SOURCE_SHA = "a" * 64
GEOMETRY_SHA = "b" * 64


def test_external_constraint_required_schema_explains_presence_and_check_semantics() -> None:
    required = ProductFamilyExternalConstraintContract.model_json_schema()["properties"][
        "required"
    ]
    assert required["default"] is True
    assert "every self_geometry invocation must supply" in required["description"]
    assert "False permits omission" in required["description"]
    assert "supplied is still independently checked" in required["description"]


def _structured_path_parameter():
    return {
        "parameter_kind": "array", "parameter_key": "points", "min_items": 2, "max_items": 128,
        "items": {
            "parameter_kind": "array",  "min_items": 3, "max_items": 3,
            "items": {"parameter_kind": "number",  "unit": "metre",
                      "minimum": -100, "maximum": 100},
        },
    }


def _tagged_shape_parameter():
    return {"parameter_kind": "tagged_object", "parameter_key": "shape",
            "discriminator_key": "mode", "branches": [
                {"parameter_kind": "object", "properties": [
                    {"parameter_kind": "choice", "parameter_key": "mode", "choices": [mode]},
                    {"parameter_kind": "number", "parameter_key": field,
                     "minimum": 0.001, "maximum": 100},
                ]} for mode, field in (("curved", "radius_m"), ("sphere", "diameter_m"))]}


def test_family_tagged_object_selects_complete_branch_and_preserves_defaults():
    data = _parameter_audit_contract_data()
    shape = _tagged_shape_parameter()
    shape.update(required=False, default_value={"mode": "sphere", "diameter_m": 0.5})
    data["public_parameters"] = [shape]
    contract = ProductFamilyContract.model_validate(data)
    assert ProductFamilyContract.model_validate_json(contract.model_dump_json()) == contract
    expected = {"shape": {"mode": "sphere", "diameter_m": 0.5}}
    result = normalize_product_family_public_parameters(contract, {})
    assert result == expected
    result["shape"]["diameter_m"] = 99
    assert normalize_product_family_public_parameters(contract, {}) == expected
    assert normalize_product_family_public_parameters(contract, {
        "shape": {"mode": "curved", "radius_m": 1}})["shape"]["radius_m"] == 1.0


@pytest.mark.parametrize("value", [
    {}, {"mode": "unknown"}, {"mode": "sphere"},
    {"mode": "sphere", "radius_m": 1},
    {"mode": "sphere", "diameter_m": 1, "radius_m": 1},
    {"mode": "sphere", "diameter_m": True},
])
def test_family_tagged_object_rejects_missing_or_mixed_branch_values(value):
    data = _parameter_audit_contract_data()
    data["public_parameters"] = [_tagged_shape_parameter()]
    contract = ProductFamilyContract.model_validate(data)
    with pytest.raises(ValueError, match="shape"):
        normalize_product_family_public_parameters(contract, {"shape": value})


@pytest.mark.parametrize("change", ["duplicate", "optional_tag", "default_tag", "multi_choice", "unbounded"])
def test_family_tagged_object_declarations_enforce_unique_tag_and_all_solved_bounds(change):
    data = _parameter_audit_contract_data()
    shape = _tagged_shape_parameter()
    tag = shape["branches"][1]["properties"][0]
    if change == "duplicate":
        tag["choices"] = ["curved"]
    elif change == "optional_tag":
        tag.update(required=False, default_value="sphere")
    elif change == "default_tag":
        tag["default_value"] = "sphere"
    elif change == "multi_choice":
        tag["choices"] = ["sphere", "other"]
    else:
        del shape["branches"][1]["properties"][1]["maximum"]
    data["solved_parameters"] = [shape]
    with pytest.raises(ValueError):
        ProductFamilyContract.model_validate(data)


def test_family_structured_parameters_roundtrip_units_defaults_and_geometry_mapping():
    from CAD2BIM_backend.domain.geometry_runtime import normalize_geometry_function_parameters

    data = _contract_data()
    data["public_parameters"].extend([
        _structured_path_parameter(),
        {"parameter_kind": "object", "parameter_key": "section", "required": False,
         "default_value": {}, "properties": [
             {"parameter_kind": "number", "parameter_key": "thickness", "unit": "metre",
              "required": False, "default_value": 0.005, "minimum": 0.001, "maximum": 0.01},
         ]},
    ])
    contract = ProductFamilyContract.model_validate(data)
    restored = ProductFamilyContract.model_validate_json(contract.model_dump_json())
    assert restored == contract
    values = {"outside_diameter": 0.1, "points": [[0, 0, 0], [1, 0, 0], [1, 2, 0]]}
    normalized = normalize_product_family_public_parameters(restored, values)
    assert normalized["section"] == {"thickness": 0.005}
    schema = {"type": "object", "additionalProperties": False, "required": ["points"],
              "properties": {"points": {"type": "array", "minItems": 2, "maxItems": 128,
                  "items": {"type": "array", "minItems": 3, "maxItems": 3,
                            "items": {"type": "number", "minimum": -100, "maximum": 100}}}}}
    projected = normalize_geometry_function_parameters(schema, {"points": normalized["points"]})
    assert projected["points"] == values["points"]
    projected["points"][1][0] = 99
    assert normalized["points"][1][0] == values["points"][1][0] == 1


@pytest.mark.parametrize("value", [
    [[0, 0, 0]], [[0, 0, 0], [1, 2]], [[0, 0, 0], [True, 2, 3]],
    [[0, 0, 0], ["1", 2, 3]], [[0, 0, 0], [101, 2, 3]], [[0, 0, 0]] * 129,
])
def test_family_structured_parameters_reject_invalid_path_values(value):
    data = _contract_data()
    data["public_parameters"].append(_structured_path_parameter())
    contract = ProductFamilyContract.model_validate(data)
    with pytest.raises(ValueError, match="points"):
        normalize_product_family_public_parameters(contract, {"outside_diameter": 0.1, "points": value})


def test_family_structured_parameters_reject_unknown_fields_and_excessive_depth():
    data = _contract_data()
    definition = {"parameter_kind": "object", "parameter_key": "section", "properties": []}
    data["public_parameters"].append(definition)
    contract = ProductFamilyContract.model_validate(data)
    with pytest.raises(ValueError, match=r"unknown key: section\.x"):
        normalize_product_family_public_parameters(contract, {"outside_diameter": 0.1, "section": {"x": 1}})
    for _ in range(4):
        definition.pop("parameter_key", None)
        definition = {"parameter_kind": "array", "parameter_key": "nested", "min_items": 1,
                      "max_items": 1, "items": definition}
    data["public_parameters"][-1] = definition
    with pytest.raises(ValueError, match="depth 4"):
        ProductFamilyContract.model_validate(data)


def _parameter_audit_contract_data():
    return current_family_document(fragment_key="shape", binding_key="geometry", public_parameters=[])


def test_solved_bounds_diagnostic_reports_peer_and_nested_paths_together():
    from CAD2BIM_backend.domain.validation_diagnostics import validation_error_message

    data = _parameter_audit_contract_data()
    data["solved_parameters"] = [
        {"parameter_kind": "number", "parameter_key": "side_a"},
        {"parameter_kind": "number_array", "parameter_key": "side_b", "length": 2},
        {"parameter_kind": "object", "parameter_key": "section", "properties": [
            {"parameter_kind": "number", "parameter_key": "radius_m"},
            {"parameter_kind": "number", "parameter_key": "offset_m"},
        ]},
        {"parameter_kind": "array", "parameter_key": "stations", "min_items": 1, "max_items": 2,
         "items": {"parameter_kind": "object", "properties": [
             {"parameter_kind": "number", "parameter_key": "x_m"},
             {"parameter_kind": "number", "parameter_key": "z_m"},
         ]}},
    ]
    with pytest.raises(ValidationError) as captured:
        ProductFamilyContract.model_validate(data)
    message = validation_error_message("invalid Family", captured.value, include_value_error_reason=True)
    assert "affected_count=6" in message
    assert all(f"solved_parameters.{path}" in message for path in (
        "side_a", "side_b", "section.radius_m", "section.offset_m",
        "stations.items.x_m", "stations.items.z_m",
    ))
    numeric_declarations = [*data["solved_parameters"][:2],
        *data["solved_parameters"][2]["properties"],
        *data["solved_parameters"][3]["items"]["properties"]]
    for parameter in numeric_declarations:
        parameter.update(minimum=0, maximum=1)
    ProductFamilyContract.model_validate(data)

    data["solved_parameters"] = [
        {"parameter_kind": "number", "parameter_key": f"unbounded_parameter_{index:03d}"}
        for index in range(64)
    ]
    with pytest.raises(ValidationError) as captured:
        ProductFamilyContract.model_validate(data)
    message = validation_error_message("invalid Family", captured.value, include_value_error_reason=True)
    assert "affected_count=64" in message
    assert len(message.split("reason=", 1)[1].encode("utf-8")) <= 512
    assert "solved_parameters.unbounded_parameter_063" not in message


def test_missing_required_peers_are_reported_before_defaults_with_unknown_keys_first():
    data = _parameter_audit_contract_data()
    data["public_parameters"] = [{
        "parameter_kind": "object", "parameter_key": "section", "properties": [
            {"parameter_kind": "number", "parameter_key": "width"},
            {"parameter_kind": "number", "parameter_key": "height"},
            {"parameter_kind": "number", "parameter_key": "wall", "required": False,
             "default_value": 0.01},
        ],
    }]
    contract = ProductFamilyContract.model_validate(data)
    with pytest.raises(ValueError) as captured:
        normalize_product_family_public_parameters(contract, {"section": {}})
    assert all(token in str(captured.value) for token in (
        "affected_count=2", "section.width", "section.height",
    ))
    assert "section.wall" not in str(captured.value)
    with pytest.raises(ValueError) as unknown:
        normalize_product_family_public_parameters(contract, {"section": {"extra": 1}})
    assert "unknown key: section.extra" in str(unknown.value)
    assert "missing required" not in str(unknown.value)
    assert normalize_product_family_public_parameters(contract, {
        "section": {"width": 0.1, "height": 0.2},
    }) == {"section": {"width": 0.1, "height": 0.2, "wall": 0.01}}


@pytest.mark.parametrize("boundary", ["depth", "nodes"])
def test_parameter_audit_family_tree_exact_boundary(boundary):
    data = _parameter_audit_contract_data()
    if boundary == "depth":
        node = {"parameter_kind": "number", "parameter_key": "value"}
        for index in range(3):
            node = {"parameter_kind": "object", "parameter_key": f"level_{index}", "properties": [node]}
        data["public_parameters"] = [node]
    else:
        # Four object nodes + 252 leaves = 256; the Family parameter tuple has no JSON schema root.
        data["public_parameters"] = [{"parameter_kind": "object", "parameter_key": f"group_{group}",
            "properties": [{"parameter_kind": "number", "parameter_key": f"field_{i}"} for i in range(count)]}
            for group, count in enumerate((64, 64, 64, 60))]
    ProductFamilyContract.model_validate(data)
    if boundary == "depth":
        data["public_parameters"] = [{"parameter_kind": "object", "parameter_key": "too_deep", "properties": data["public_parameters"]}]
    else:
        data["public_parameters"][-1]["properties"].append({"parameter_kind": "number", "parameter_key": "extra"})
    with pytest.raises(ValueError, match="depth 4 or 256 nodes"):
        ProductFamilyContract.model_validate(data)


def test_parameter_audit_family_nested_defaults_normalize_and_remain_owned():
    data = _parameter_audit_contract_data()
    data["public_parameters"] = [{"parameter_kind": "object", "parameter_key": "section", "required": False,
        "default_value": {}, "properties": [
            {"parameter_kind": "number", "parameter_key": "width", "unit": "metre", "required": False,
             "minimum": 0.1, "maximum": 0.5, "default_value": 0.25},
            {"parameter_kind": "array", "parameter_key": "flags", "required": False, "default_value": [True],
             "min_items": 1, "max_items": 2, "items": {"parameter_kind": "boolean"}},
        ]}]
    contract = ProductFamilyContract.model_validate(data)
    normalized = normalize_product_family_public_parameters(contract, {})
    assert normalized == normalize_product_family_public_parameters(
        contract, {"section": {"flags": [True], "width": 0.25}}
    )
    assert normalize_product_family_public_parameters(contract, normalized) == normalized
    assert contract.public_parameters[0].properties[0].unit.value == "metre"
    normalized["section"]["flags"].append(False)
    assert normalize_product_family_public_parameters(contract, {})["section"]["flags"] == [True]
    assert data["public_parameters"][0]["default_value"] == {}


@pytest.mark.parametrize("definition", [
    {"parameter_kind": "number", "unit": "millimetre"},
    {"parameter_kind": "number", "minimum": 1, "maximum": 0},
    {"parameter_kind": "number", "minimum": float("nan")},
    {"parameter_kind": "number", "minimum": 0, "maximum": 1, "default_value": 2},
    {"parameter_kind": "integer", "unit": "metre"},
    {"parameter_kind": "boolean", "required": False},
    {"parameter_kind": "choice", "choices": ["same", "same"]},
    {"parameter_kind": "object", "properties": [], "additionalProperties": True},
    {"parameter_kind": "array", "min_items": 0, "max_items": 129, "items": {"parameter_kind": "number"}},
    {"parameter_kind": "array", "min_items": 0, "max_items": 2, "items": {"parameter_kind": "number", "required": False}},
])
def test_parameter_audit_family_typed_declarations_remain_closed(definition):
    data = _parameter_audit_contract_data()
    data["public_parameters"] = [{"parameter_key": "parameter", **definition}]
    with pytest.raises(ValueError):
        ProductFamilyContract.model_validate(data)


@pytest.mark.parametrize("value", [True, "0.5", None, float("nan"), float("inf"), -0.001, 1.001])
def test_parameter_audit_family_number_values_preserve_units_type_and_range(value):
    data = _parameter_audit_contract_data()
    data["public_parameters"] = [{"parameter_kind": "number", "parameter_key": "angle", "unit": "radian", "minimum": 0, "maximum": 1}]
    contract = ProductFamilyContract.model_validate(data)
    assert normalize_product_family_public_parameters(contract, {"angle": 0}) == {"angle": 0.0}
    assert normalize_product_family_public_parameters(contract, {"angle": 1}) == {"angle": 1.0}
    with pytest.raises(ValueError, match="angle"):
        normalize_product_family_public_parameters(contract, {"angle": value})


def test_parameter_audit_family_exact_64k_parameter_budget():
    data = _parameter_audit_contract_data()
    data["public_parameters"] = [{"parameter_kind": "array", "parameter_key": "samples", "min_items": 0, "max_items": 128,
        "items": {"parameter_kind": "array",  "min_items": 0, "max_items": 128,
            "items": {"parameter_kind": "integer",  "minimum": 0, "maximum": 1000}}}]
    contract = ProductFamilyContract.model_validate(data)
    # Build independently counted, schema-valid integer rows, then tune 1–4 bytes without exceeding any item bound.
    values = {"samples": [[1000] * 128 for _ in range(101)] + [[]]}
    size = len(canonical_json(values).encode("utf-8"))
    while size < 65_536:
        if len(values["samples"][-1]) == 128:
            values["samples"].append([])
            size += 3  # comma plus the new empty row brackets
        size += 5 if values["samples"][-1] else 4  # optional comma plus four digits
        values["samples"][-1].append(1000)
    excess = size - 65_536
    for index in range(excess):
        values["samples"][0][index] = 100
    assert len(canonical_json(values).encode("utf-8")) == 65_536
    assert normalize_product_family_public_parameters(contract, values) == values
    values["samples"][-1].append(0)
    with pytest.raises(ValueError, match="64 KiB"):
        normalize_product_family_public_parameters(contract, values)


@pytest.mark.parametrize("change", ["missing_component", "unknown_owner", "missing_sources", "unknown_public", "unknown_solved", "duplicate_public", "duplicate_solved", "unbounded_solved", "nested_unbounded_solved", "unbounded_number_array"])
def test_parameter_audit_family_v4_requires_closed_source_ownership(change):
    data = _parameter_audit_contract_data()
    scalar = {"parameter_kind": "number", "parameter_key": "length", "unit": "metre", "minimum": 0.1, "maximum": 2}
    data["public_parameters"] = [scalar]
    if change == "missing_component":
        data["components"] = []
    elif change == "unknown_owner":
        data["geometry_fragments"][0]["owner_component_key"] = "unknown"
    elif change == "missing_sources":
        data["geometry_fragments"][0]["binding_options"][0].pop("parameter_sources")
    elif change in {"unknown_public", "unknown_solved"}:
        data["geometry_fragments"][0]["binding_options"][0]["parameter_sources"] = {"input": {"source_kind": "public_parameter" if change == "unknown_public" else "solved_parameter", "parameter_key": "unknown"}}
    elif change == "duplicate_public":
        data["public_parameters"] = [scalar, scalar]
    elif change == "duplicate_solved":
        data["solved_parameters"] = [scalar, scalar]
    elif change == "unbounded_number_array":
        data["solved_parameters"] = [{"parameter_kind": "number_array", "parameter_key": "axis",
                                       "length": 3, "minimum": -1}]
    else:
        unbounded = {"parameter_kind": "number", "parameter_key": "value", "minimum": 0}
        data["solved_parameters"] = [unbounded if change == "unbounded_solved" else
            {"parameter_kind": "object", "parameter_key": "options", "properties": [unbounded]}]
    with pytest.raises(ValueError) as captured:
        ProductFamilyContract.model_validate(data)
    if change in {"unknown_public", "unknown_solved"}:
        assert all(key in str(captured.value) for key in ("shape", "geometry", "input", "unknown"))
    elif change in {"unbounded_solved", "nested_unbounded_solved", "unbounded_number_array"}:
        path = {"unbounded_solved": "solved_parameters.value",
                "nested_unbounded_solved": "solved_parameters.options.value",
                "unbounded_number_array": "solved_parameters.axis"}[change]
        assert path in str(captured.value)
        assert "minimum and maximum" in str(captured.value)


@pytest.mark.parametrize("source", [
    {"source_kind": "fixed", "value": 0}, {"source_kind": "fixed", "value": -1},
    {"source_kind": "fixed", "value": True}, {"source_kind": "fixed", "value": "1"},
    {"source_kind": "fixed", "value": float("inf")},
    {"source_kind": "public_parameter", "parameter_key": "length"},
])
def test_parameter_audit_family_uniform_scale_is_positive_dimensionless(source):
    data = _parameter_audit_contract_data()
    default = ProductFamilyContract.model_validate(data).geometry_fragments[0].binding_options[0].uniform_scale_source
    assert default.source_kind == "fixed" and default.value == 1.0
    data["public_parameters"] = [{"parameter_kind": "number", "parameter_key": "length", "unit": "metre", "minimum": 0.1, "maximum": 2}]
    data["geometry_fragments"][0]["binding_options"][0]["uniform_scale_source"] = source
    with pytest.raises(ValueError):
        ProductFamilyContract.model_validate(data)


def test_parameter_sources_preserve_explicit_fixed_public_and_solved_ownership():
    from hashlib import sha256

    from CAD2BIM_backend.domain.product_realization import (
        ProductRealizationInvocation,
        ProductRealizationRequest,
        ResolvedModelingFunctionBinding,
        SolvedProductRealizationAuthorProposal,
        validate_product_realization_proposal,
    )

    data = _parameter_audit_contract_data()
    wall = {"parameter_kind": "number", "parameter_key": "wall_m", "unit": "metre", "minimum": 0.0002, "maximum": 0.02}
    data["public_parameters"] = [wall]
    data["solved_parameters"] = [wall]
    data["geometry_fragments"][0]["binding_options"][0]["parameter_sources"] = {"overrides": {
        "requested_wall_m": {"source_kind": "public_parameter", "parameter_key": "wall_m"},
        "effective_wall_m": {"source_kind": "solved_parameter", "parameter_key": "wall_m"},
        "label": {"source_kind": "fixed", "value": {"kind": "body"}},
    }}
    contract = ProductFamilyContract.model_validate(data)
    assert ProductFamilyContract.model_validate_json(contract.model_dump_json()) == contract
    with pytest.raises(ValueError, match="unknown key: length"):
        normalize_product_family_public_parameters(contract, {"wall_m": 0.003, "length": 1})

    source = b"parameter mapping contract witness; no program is executed"
    binding = ResolvedModelingFunctionBinding(
        binding_key="geometry", geometry_content_digest="a" * 64,
        source_bundle=source, source_bundle_sha256=sha256(source).hexdigest(),
        parameter_schema={"type": "object", "additionalProperties": False,
            "required": ["requested_wall_m", "effective_wall_m", "label"],
            "properties": {
                "requested_wall_m": {"type": "number", "minimum": 0.0002, "maximum": 0.02},
                "effective_wall_m": {"type": "number", "minimum": 0.0002, "maximum": 0.02},
                "label": {"type": "object", "additionalProperties": False,
                    "required": ["kind"], "properties": {"kind": {"type": "string", "enum": ["body"]}}},
            }},
    )
    request = ProductRealizationRequest(
        family_contract=contract, family_contract_digest=sha256_digest(contract),
        source_bundle=source, source_bundle_sha256=sha256(source).hexdigest(),
        geometry_bindings=(binding,),
        invocation=ProductRealizationInvocation(public_parameters={"wall_m": 0.003},
            product_origin=(0, 0, 0), length_unit=IfcLengthUnit()),
    )
    proposal = SolvedProductRealizationAuthorProposal.model_validate({
        "status": "SOLVED", "internal_parameters": {"wall_m": 0.005},
        "components": [{"component_key": "body"}],
        "fragments": [{"fragment_key": "shape", "binding_key": "geometry"}],
    })
    resolved = validate_product_realization_proposal(request, proposal)
    assert resolved.fragments[0].geometry_parameters == {
        "requested_wall_m": 0.003, "effective_wall_m": 0.005, "label": {"kind": "body"},
    }


def test_branch_only_solved_values_can_be_absent_without_weakening_public_or_nested_defaults():
    from CAD2BIM_backend.domain.product_family import normalize_product_family_parameter_values

    data = _parameter_audit_contract_data()
    branch_shape = {
        "parameter_kind": "object", "parameter_key": "branch_shape", "required": False,
        "properties": [{
            "parameter_kind": "number", "parameter_key": "radius_m", "unit": "metre",
            "minimum": 0.01, "maximum": 1.0,
        }],
    }
    data["solved_parameters"] = [branch_shape]
    contract = ProductFamilyContract.model_validate(data)
    assert normalize_product_family_parameter_values(contract.solved_parameters, {}) == {}
    assert normalize_product_family_parameter_values(
        contract.solved_parameters, {"branch_shape": {"radius_m": 0.1}},
    ) == {"branch_shape": {"radius_m": 0.1}}

    data["public_parameters"] = [branch_shape]
    data["solved_parameters"] = []
    with pytest.raises(ValueError, match="optional public or nested"):
        ProductFamilyContract.model_validate(data)

    data["public_parameters"] = []
    data["solved_parameters"] = [{
        **branch_shape,
        "properties": [{
            **branch_shape["properties"][0], "required": False,
        }],
    }]
    with pytest.raises(ValueError, match="optional public or nested"):
        ProductFamilyContract.model_validate(data)


def test_parameter_audit_fixed_source_uses_exact_utf8_budget_and_v1_rejects_modern_fields():
    from CAD2BIM_backend.domain.product_family import ProductFamilyFixedParameterSource

    value = "é" * 8191
    assert len(canonical_json(value).encode("utf-8")) == 16_384
    assert ProductFamilyFixedParameterSource(value=value).value == value
    with pytest.raises(ValueError, match="16 KiB"):
        ProductFamilyFixedParameterSource(value=value + "x")
    data = _parameter_audit_contract_data()
    data["schema"] = "product_family_contract_v1"
    with pytest.raises(ValueError, match="extra_forbidden"):
        ProductFamilyContract.model_validate(data)


def test_parameter_audit_public_and_solved_share_one_combined_node_budget():
    data = _parameter_audit_contract_data()
    data["public_parameters"] = [{"parameter_kind": "object", "parameter_key": f"group_{group}",
        "properties": [{"parameter_kind": "number", "parameter_key": f"field_{i}"} for i in range(count)]}
        for group, count in enumerate((64, 64, 64, 59))]
    data["solved_parameters"] = [{"parameter_kind": "number", "parameter_key": "solved", "minimum": 0, "maximum": 1}]
    # 255 public nodes plus one solved node fits. The two forests do not each get 256 nodes.
    ProductFamilyContract.model_validate(data)
    data["public_parameters"][-1]["properties"].append({"parameter_kind": "number", "parameter_key": "extra"})
    with pytest.raises(ValueError, match="256 nodes"):
        ProductFamilyContract.model_validate(data)


@pytest.mark.parametrize("patch", [{"minimum": 0}, {"minimum": -0.1}, {"maximum": None}, {"parameter_kind": "integer"}])
def test_parameter_audit_variable_scale_has_an_explicit_bounded_dimensionless_domain(patch):
    data = _parameter_audit_contract_data()
    scale = {"parameter_kind": "number", "parameter_key": "scale", "unit": "dimensionless", "minimum": 0.5, "maximum": 2}
    data["public_parameters"] = [scale]
    data["geometry_fragments"][0]["binding_options"][0]["uniform_scale_source"] = {"source_kind": "public_parameter", "parameter_key": "scale"}
    ProductFamilyContract.model_validate(data)
    scale.update(patch)
    if patch.get("parameter_kind") == "integer":
        scale.pop("unit")
        scale["minimum"] = 1
    with pytest.raises(ValueError, match="bounded positive dimensionless number"):
        ProductFamilyContract.model_validate(data)


@pytest.mark.parametrize("failure", ["public_parameter", "external_requirement"])
def test_parameter_audit_v3_semantic_errors_keep_safe_reason_path_and_correction(failure):
    sentinel = "private-value-must-not-appear-4826" if failure == "public_parameter" else "0.7348269365"
    data = _parameter_audit_contract_data()
    data["public_parameters"] = [{"parameter_kind": "number", "parameter_key": "width", "unit": "metre", "minimum": 0.1, "maximum": 1}]
    values = {"width": sentinel if failure == "public_parameter" else float(sentinel)}
    if failure == "external_requirement":
        data["interfaces"] = [{"interface_key": "outlet", "owner_component_key": "body", "interface_kind": "external",
            "source": {"source_kind": "model_interface", "fragment_key": "shape", "model_interface_key": "outlet"},
            "external_constraint": {"allowed_kinds": ["fixed_point"], "allowed_directions": ["same"],
                "maximum_position_tolerance_m": 0.001, "maximum_angular_tolerance_rad": 0.001}}]
    contract = ProductFamilyContract.model_validate(data)
    scenario = ProductRealizationValidationScenario(scenario_key="parameter_audit", expected_status="SOLVED",
        realization_kind="self_geometry", public_parameters=values,
        product_origin=(0, 0, 0), length_unit=IfcLengthUnit())
    with pytest.raises(ProductFamilyInvocationSemanticError) as error:
        normalize_product_family_validation_scenario(contract, scenario)
    expected_reason, expected_path = (("PUBLIC_PARAMETER_INVALID", "public_parameters") if failure == "public_parameter"
                                      else ("SELF_GEOMETRY_REQUIREMENT_INVALID", "external_constraints"))
    assert error.value.reason == expected_reason
    assert error.value.invalid_path == expected_path
    assert error.value.correction and "exact Family" in error.value.correction
    assert sentinel not in str(error.value) + error.value.correction
    translated = error.value.for_realization_invocation()
    assert (translated.reason, translated.invalid_path, translated.correction) == (expected_reason, expected_path, error.value.correction)
    if failure == "public_parameter":
        # The invalid value is repaired at the public typed boundary; no requirement was relaxed.
        repaired = scenario.model_copy(update={"public_parameters": {"width": 0.5}})
        assert normalize_product_family_validation_scenario(contract, repaired).public_parameters == {"width": 0.5}


def _contract_data() -> dict[str, object]:
    return current_family_document(
        standard_model={"public_parameters": {"outside_diameter": 0.1}},
        parameter_sources={"diameter": {"source_kind": "public_parameter", "parameter_key": "outside_diameter"}},
        interfaces=[{
            "interface_key": "inlet", "owner_component_key": "body", "interface_kind": "external",
            "source": {"source_kind": "model_interface", "fragment_key": "body_shape", "model_interface_key": "inlet"},
            "connection_profile": {"profile_kind": "circular", "outer_diameter_parameter_key": "outside_diameter"},
            "external_constraint": {"required": False, "allowed_kinds": ["fixed_point"], "allowed_directions": ["opposed"],
                "maximum_position_tolerance_m": 0.001, "maximum_angular_tolerance_rad": 0.01},
        }],
        public_parameters=[
            {"parameter_kind": "number", "parameter_key": "outside_diameter", "unit": "metre", "minimum": 0.01, "maximum": 2.0},
            {"parameter_kind": "choice", "parameter_key": "orientation", "required": False,
             "choices": ["up", "down"], "default_value": "up"},
        ],
    )

def _contract() -> ProductFamilyContract:
    return ProductFamilyContract.model_validate(_contract_data())


def _draft_command(*, usage="Test Family usage instructions."):
    binding = _resolved_binding()
    return FamilyDraftCreateCommand(mode="create", display_name="Regulator family",
        purpose="Join one circular gas interface", usage_instructions={"action": "set", "value": usage},
        source={"source_kind": "inline", "source_files": [{"path": "main.py", "source": "def realize(ctx):\n    return None\n"}],
            "definition": {"action": "set", "value": {"family_contract": _contract_data(),
                "geometry_bindings": [{"binding_key": binding.binding_key,
                    "source": {"source_kind": "published", "resource_version_alias": "cur.exact-geometry"},
                    "expected_resource_kind": binding.expected_resource_kind,
                    "geometry_purpose": binding.geometry_purpose}]}}})


def _scenario() -> ProductRealizationValidationScenario:
    return ProductRealizationValidationScenario(
        scenario_key="nominal", expected_status=ProductRealizationStatus.SOLVED,
        realization_kind="self_geometry",
        public_parameters={"outside_diameter": 0.1, "orientation": "up"},
        product_origin=(0, 0, 0),
        length_unit=IfcLengthUnit(),
    )



def _resolved_binding(
    *,
    owner_scope: ResourceOwnerScope | None = None,
) -> ResolvedProductFamilyGeometryBindingInput:
    return ResolvedProductFamilyGeometryBindingInput(
        binding_key="body_geometry",
        geometry_resource_id="geometry-1",
        geometry_resource_version_id="geometry-version-1",
        geometry_content_digest=GEOMETRY_SHA,
        expected_resource_kind=GeometryResourceKind.MODELING_FUNCTION,
        geometry_purpose=ProductFamilyGeometryPurpose.PRODUCT_COMPONENT,
        geometry_owner_scope=owner_scope
        or ResourceOwnerScope(scope_kind=ResourceOwnerScopeKind.SYSTEM),
    )


def _source_asset() -> ProductFamilyVersionAssetRecord:
    return ProductFamilyVersionAssetRecord(
        family_version_id="family-version-1",
        asset_role=ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE,
        asset_format="source_bundle_v1",
        filename="source.zip",
        content_type="application/zip",
        size_bytes=128,
        sha256=SOURCE_SHA,
        created_at=NOW,
    )


def _formal_assets():
    source = _source_asset()
    return (source,
            source.model_copy(update={"asset_role": ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY,
                "asset_format": "occt_brep_v1", "content_type": "application/vnd.opencascade.brep", "filename": "standard.brep"}),
            source.model_copy(update={"asset_role": ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST,
                "asset_format": "json", "content_type": "application/json", "filename": "standard.json"}))


def _draft() -> ProductFamilyAuthoringVersionDraft:
    contract = ProductFamilyContract.model_validate(_contract_data())
    binding = _resolved_binding()
    contract_digest = sha256_digest(contract)
    return ProductFamilyAuthoringVersionDraft(
        family_version_id="family-version-1",
        family_id="family-1",
        display_name="Regulator family",
        purpose="Join one circular gas interface",
        usage_instructions="Test Family usage instructions.",
        family_contract=contract,
        contract_digest=contract_digest,
        geometry_bindings=(binding,),
        created_at=NOW,
    )


def _operation(
    *,
    draft: ProductFamilyAuthoringVersionDraft | None = None,
    status: CriticalOperationStatus = CriticalOperationStatus.SUCCEEDED,
) -> CriticalOperationRecord:
    draft = draft or _draft()
    succeeded = status is CriticalOperationStatus.SUCCEEDED
    return CriticalOperationRecord(
        operation_id="operation-1",
        owner_id="owner-1",
        tenant_id="tenant-1",
        task_id="task-1",
        run_id="run-1",
        operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
        initiator=CriticalOperationInitiator.DIRECT_CLIENT,
        tool_name="ifc_authoring_publish",
        intent_summary="Create a ProductFamily version.",
        idempotency_key="idempotency-1",
        request_fingerprint="c" * 64,
        status=status,
        error_code=None if succeeded else "PROGRAM_INVALID",
        error_message=None if succeeded else "Program failed validation",
        error_phase=None if succeeded else CriticalOperationErrorPhase.CODE_VALIDATION,
        code_artifact=ArtifactVersionRef(
            artifact_id="artifact-1",
            version_id="artifact-version-1",
            sha256=SOURCE_SHA,
        ),
        detail=AuthoringPublishOperationDetail(root_kind="family", root_id="family-1",
            expected_drafts=(AuthoringDraftSelection(root_kind="family", root_id="family-1", generation=1,
                snapshot=ArtifactVersionRef(artifact_id="snapshot", version_id="snapshot-v1", sha256="d" * 64)),),
            preview_examples=(AuthoringPreviewExampleReference(origin_run_id="run-1", operation_id="preview-operation-1", case_keys=("nominal",)),),
            family_version=draft if succeeded else None),
        target_product_family_id="family-1",
        result_product_family_version_id=draft.family_version_id if succeeded else None,
        started_at=NOW,
        finished_at=NOW,
    )


def _family(
    *,
    owner_scope: ResourceOwnerScope | None = None,
) -> ProductFamilyRecord:
    return ProductFamilyRecord(
        family_id="family-1",
        owner_scope=owner_scope
        or ResourceOwnerScope(
            scope_kind=ResourceOwnerScopeKind.TASK,
            tenant_id="tenant-1",
            task_id="task-1",
        ),
        created_at=NOW,
    )


def test_legacy_contract_is_rejected_without_mutating_historical_payload() -> None:
    legacy = {
        "schema": "product_family_contract_v1", "product_structure_kind": "single_product",
        "components": [{"component_key": "body", "ifc_class": "IfcValve"}],
        "geometry_fragments": [{"fragment_key": "body_shape", "owner_component_key": "body", "binding_key": "body_geometry"}],
        "interfaces": [],
    }
    before = canonical_json(legacy)
    with pytest.raises(ValidationError, match="extra_forbidden|binding_options"):
        ProductFamilyContract.model_validate(legacy)
    assert canonical_json(legacy) == before
    for patch in ({"schema": "product_family_contract_v2"}, {}):
        candidate = {**legacy, **patch}
        if not patch:
            candidate.pop("schema")
        with pytest.raises(ValidationError, match="extra_forbidden|binding_options"):
            ProductFamilyContract.model_validate(candidate)

    obsolete_anchor = _contract_data()
    obsolete_anchor["installation_anchor"] = {
        "source_kind": "model_interface",
        "fragment_key": "body_shape",
        "model_interface_key": "inlet",
    }
    with pytest.raises(ValidationError, match="installation_anchor|Extra inputs"):
        ProductFamilyContract.model_validate(obsolete_anchor)

def test_contract_closes_receipt_sources_profiles_and_external_requirements() -> None:
    contract = _contract()
    assert contract.interfaces[0].connection_profile.outer_diameter_parameter_key == "outside_diameter"
    for patch in (
        {"attachment_role_key": "side_a"},
        {"declared_frame": RigidTransform3D().model_dump()},
        {"source": {"source_kind": "program_frame"}},
        {"source": {"source_kind": "model_interface", "fragment_key": "missing", "model_interface_key": "inlet"}},
        {"connection_profile": {"profile_kind": "circular", "outer_diameter_parameter_key": "orientation"}},
    ):
        data = _contract_data()
        data["interfaces"][0].update(patch)
        with pytest.raises(ValidationError):
            ProductFamilyContract.model_validate(data)
    data = _contract_data()
    data["interfaces"][0]["external_constraint"]["allowed_kinds"] = ["fixed_point", "fixed_point"]
    with pytest.raises(ValidationError, match="modes must be unique"):
        ProductFamilyContract.model_validate(data)
    with pytest.raises(ValidationError, match="datum_transform|Extra inputs"):
        ProductRealizationValidationScenario.model_validate({
            **_scenario().model_dump(), "datum_transform": RigidTransform3D().model_dump(),
        })

def test_draft_create_is_closed_and_static_source_validation_does_not_execute():
    command = _draft_command()
    parsed = TypeAdapter(FamilyDraftWriteCommand).validate_python(command.model_dump())
    assert parsed == command
    validate_draft_source({"main.py": b"raise RuntimeError('must not execute')\ndef realize(ctx): pass\ndef standard_model(ctx): pass"},
        entry_functions=("realize", "standard_model"))
    with pytest.raises(AuthoringDraftError, match="Source requires main.py"):
        validate_draft_source({"solver.py": b"VALUE = 1"}, entry_functions=("realize",))
    with pytest.raises(ValidationError, match="Extra inputs"):
        FamilyDraftCreateCommand.model_validate(command.model_dump() | {"validation_scenarios": []})


def test_draft_update_inherits_omitted_fields_and_requires_current_edit_token():
    command = FamilyDraftUpdateCommand(mode="update", draft_ref="cur.draft", expected_edit_token=2,
        purpose="Revised usage description")
    assert command.source_changes is None and command.definition is None
    for invalid in ({"expected_edit_token": True}, {"source_changes": None}):
        with pytest.raises(ValidationError):
            FamilyDraftUpdateCommand.model_validate(command.model_dump(exclude_unset=True) | invalid)
    with pytest.raises(ValidationError, match="requires changes"):
        FamilyDraftUpdateCommand(mode="update", draft_ref="cur.draft", expected_edit_token=2)




def test_create_usage_instructions_are_required_bounded_and_exactly_preserved() -> None:
    command = _draft_command(usage="  ## Purpose\nKeep intentional whitespace.  ")
    assert command.usage_instructions.value == "  ## Purpose\nKeep intentional whitespace.  "

    document = command.model_dump(mode="json")
    for invalid in (None, "", " \n\t", "contains\x00nul", "x" * 8193):
        candidate = dict(document)
        candidate["usage_instructions"] = None if invalid is None else {"action": "set", "value": invalid}
        with pytest.raises(ValidationError, match="usage_instructions"):
            FamilyDraftCreateCommand.model_validate(candidate)

    exact_limit = dict(document)
    exact_limit["usage_instructions"] = {"action": "set", "value": "𐍈" * 8192}
    assert len(
        FamilyDraftCreateCommand.model_validate(exact_limit)
        .usage_instructions.value.encode("utf-8")
    ) == 32768


def test_public_parameter_and_scenario_normalization_is_single_and_closed() -> None:
    contract = _contract()
    normalized = normalize_product_family_public_parameters(contract, {"outside_diameter": 0.1})
    assert normalized == {"outside_diameter": 0.1, "orientation": "up"}
    with pytest.raises(ValueError, match="missing required keys.*outside_diameter"):
        normalize_product_family_public_parameters(contract, {})
    raw = _scenario().model_copy(update={"public_parameters": {"outside_diameter": 0.1}})
    assert normalize_product_family_validation_scenario(contract, raw).public_parameters == normalized
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProductFamilyAuthoringVersionDraft.model_validate(_draft().model_dump() | {"validation_scenarios": (raw,)})
    for obsolete in ("attachments", "interface_bindings", "predicted_host_edit"):
        with pytest.raises(ValidationError, match="Extra inputs"):
            ProductRealizationValidationScenario.model_validate(_scenario().model_dump() | {obsolete: []})
    with pytest.raises(ValidationError, match="self_geometry"):
        ProductRealizationValidationScenario.model_validate(_scenario().model_dump() | {"realization_kind": "connected"})

def test_external_scenario_requirements_keep_provenance_modes_and_tolerance_bounds() -> None:
    condition = {
        "kind": "fixed_point", "point_m": [0, 0, 0], "axis": [1, 0, 0],
        "direction_relation": "opposed", "outer_diameter_m": 0.1,
        "position_tolerance_m": 0.001, "angular_tolerance_rad": 0.01,
    }
    requirement = {"interface_key": "inlet", "source_kind": "design_target", "condition": condition}
    scenario = ProductRealizationValidationScenario.model_validate(_scenario().model_dump() | {"external_constraints": [requirement]})
    assert normalize_product_family_validation_scenario(_contract(), scenario) == scenario
    for patch in ({"interface_key": "missing"}, {"condition": {**condition, "position_tolerance_m": 0.002}},
                  {"condition": {**condition, "angular_tolerance_rad": 0.02}},
                  {"condition": {**condition, "kind": "infinite_axis"}},
                  {"condition": {**condition, "direction_relation": "same"}}):
        invalid = ProductRealizationValidationScenario.model_validate(_scenario().model_dump() | {"external_constraints": [{**requirement, **patch}]})
        with pytest.raises(ProductFamilyInvocationSemanticError) as error:
            normalize_product_family_validation_scenario(_contract(), invalid)
        assert error.value.reason == "SELF_GEOMETRY_REQUIREMENT_INVALID"
        assert error.value.invalid_path == "external_constraints"
    for required in ("roll_required", "profile_required"):
        data = _contract_data()
        data["interfaces"][0]["external_constraint"][required] = True
        incomplete = scenario.model_dump()
        incomplete["external_constraints"][0]["condition"].pop("outer_diameter_m")
        with pytest.raises(ProductFamilyInvocationSemanticError, match="cannot be relaxed"):
            normalize_product_family_validation_scenario(ProductFamilyContract.model_validate(data),
                ProductRealizationValidationScenario.model_validate(incomplete))
    with pytest.raises(ValidationError, match="unique"):
        ProductRealizationValidationScenario.model_validate(_scenario().model_dump() | {"external_constraints": [requirement, requirement]})
    with pytest.raises(ValidationError, match="self_geometry"):
        ProductRealizationValidationScenario.model_validate(scenario.model_dump() | {"realization_kind": "standard_model"})
    with pytest.raises(ValidationError, match="evidence reference"):
        ProductRealizationValidationScenario.model_validate(_scenario().model_dump() | {"external_constraints": [{**requirement, "source_kind": "observed_interface"}]})

def test_choice_defaults_and_optional_external_requirements_are_closed() -> None:
    data = _contract_data()
    data["public_parameters"][1]["choices"] = ["up", " up "]
    with pytest.raises(ValidationError, match="choices must be unique"):
        ProductFamilyContract.model_validate(data)
    assert normalize_product_family_validation_scenario(_contract(), _scenario()) == _scenario()
    required = _contract_data()
    required["interfaces"][0]["external_constraint"]["required"] = True
    with pytest.raises(ProductFamilyInvocationSemanticError) as error:
        normalize_product_family_validation_scenario(ProductFamilyContract.model_validate(required), _scenario())
    assert error.value.reason == "SELF_GEOMETRY_REQUIREMENT_INVALID"





def test_version_draft_retains_exact_reopen_inputs_without_geometry_assets() -> None:
    draft = _draft()
    persisted = draft.model_dump(mode="json")
    reopened = ProductFamilyAuthoringVersionDraft.model_validate(persisted)
    assert reopened == draft
    assert reopened.geometry_bindings[0].geometry_resource_version_id == "geometry-version-1"
    assert "validation_scenarios" not in persisted
    assert persisted["family_contract"]["standard_model"] == {"public_parameters": {"outside_diameter": 0.1}}
    assert "assets" not in persisted["geometry_bindings"][0]


@pytest.mark.parametrize("standard", [{}, {"public_parameters": {}}, {"public_parameters": {"outside_diameter": 0.1}}])
def test_standard_parameter_serialization_preserves_omission_in_draft_declarations(standard) -> None:
    command = _draft_command().model_dump(mode="json", by_alias=True)
    command["source"]["definition"]["value"]["family_contract"]["standard_model"] = standard
    parsed = FamilyDraftCreateCommand.model_validate(command)
    persisted = parsed.model_dump(mode="json", exclude_unset=True, by_alias=True)
    actual = persisted["source"]["definition"]["value"]["family_contract"]["standard_model"]
    expected = standard if standard.get("public_parameters") else {}
    assert actual == expected
    assert FamilyDraftCreateCommand.model_validate(persisted) == parsed


def test_publication_closes_task_operation_exact_bindings_and_source_asset() -> None:
    draft = _draft()
    publication = ProductFamilyAuthoringPublication(
        operation=_operation(draft=draft),
        family=_family(),
        version=draft,
        geometry_bindings=draft.geometry_bindings,
        assets=_formal_assets(),
    )
    bundle = publication.to_bundle(version_no=1)
    assert bundle.version.family_version_id == "family-version-1"
    assert bundle.geometry_bindings[0].geometry_resource_version_id == "geometry-version-1"

    tenant_family = _family(owner_scope=ResourceOwnerScope(
        scope_kind=ResourceOwnerScopeKind.TENANT, tenant_id="tenant-1"))
    tenant_publication = ProductFamilyAuthoringPublication(
        operation=publication.operation, family=tenant_family, version=draft,
        geometry_bindings=draft.geometry_bindings, assets=publication.assets)
    assert tenant_publication.to_bundle(version_no=1).family.owner_scope == tenant_family.owner_scope

    with pytest.raises(ValidationError, match="Family tenant scope does not match"):
        ProductFamilyAuthoringPublication(
            operation=publication.operation,
            family=_family(
                owner_scope=ResourceOwnerScope(
                    scope_kind=ResourceOwnerScopeKind.TENANT,
                    tenant_id="tenant-other",
                )
            ),
            version=draft,
            geometry_bindings=draft.geometry_bindings,
            assets=publication.assets,
        )

    with pytest.raises(ValidationError, match="exact source asset"):
        ProductFamilyAuthoringPublication(
            operation=publication.operation,
            family=publication.family,
            version=draft,
            geometry_bindings=draft.geometry_bindings,
            assets=publication.assets[1:],
        )

    wrong_result = publication.operation.model_copy(
        update={"result_product_family_version_id": "family-version-other"}
    )
    with pytest.raises(ValidationError, match="result differs from its saved plan"):
        ProductFamilyAuthoringPublication(
            operation=wrong_result,
            family=publication.family,
            version=draft,
            geometry_bindings=draft.geometry_bindings,
            assets=publication.assets,
        )

    wrong_base = publication.operation.model_copy(
        update={"base_product_family_version_id": "family-version-base"}
    )
    with pytest.raises(ValidationError, match="result differs from its saved plan"):
        ProductFamilyAuthoringPublication(
            operation=wrong_base,
            family=publication.family,
            version=draft,
            geometry_bindings=draft.geometry_bindings,
            assets=publication.assets,
        )

    cross_task_binding = _resolved_binding(
        owner_scope=ResourceOwnerScope(
            scope_kind=ResourceOwnerScopeKind.TASK,
            tenant_id="tenant-1",
            task_id="task-2",
        )
    )
    cross_task_draft = draft.model_copy(update={"geometry_bindings": (cross_task_binding,)})
    cross_task_operation = _operation(draft=cross_task_draft)
    with pytest.raises(ValidationError, match="geometry binding crosses its owner scope"):
        ProductFamilyAuthoringPublication(
            operation=cross_task_operation,
            family=publication.family,
            version=cross_task_draft,
            geometry_bindings=cross_task_draft.geometry_bindings,
            assets=publication.assets,
        )
    with pytest.raises(ValidationError, match="geometry binding crosses its owner scope"):
        ProductFamilyAuthoringPublication(
            operation=cross_task_operation, family=tenant_family, version=cross_task_draft,
            geometry_bindings=cross_task_draft.geometry_bindings, assets=publication.assets)


def test_publication_bundle_uses_canonical_binding_order_for_exact_reopen() -> None:
    contract_data = _contract_data()
    contract_data["geometry_fragments"] = [
        {
            "fragment_key": "z_shape",
            "owner_component_key": "body",
            "binding_options": [{"binding_key": "z_geometry", "parameter_sources": {"overrides": {}}}],
        },
        {
            "fragment_key": "a_shape",
            "owner_component_key": "body",
            "binding_options": [{"binding_key": "a_geometry", "parameter_sources": {"overrides": {}}}],
        },
    ]
    contract_data["interfaces"][0]["source"]["fragment_key"] = "z_shape"
    contract = ProductFamilyContract.model_validate(contract_data)
    z_binding = _resolved_binding().model_copy(update={"binding_key": "z_geometry"})
    a_binding = _resolved_binding().model_copy(
        update={
            "binding_key": "a_geometry",
            "geometry_resource_id": "geometry-2",
            "geometry_resource_version_id": "geometry-version-2",
        }
    )
    draft = ProductFamilyAuthoringVersionDraft(
        family_version_id="family-version-1",
        family_id="family-1",
        display_name="Ordered assembly",
        usage_instructions="Ordered binding example.",
        purpose="Close publication ordering before exact database reopen.",
        family_contract=contract,
        contract_digest=sha256_digest(contract),
        geometry_bindings=(z_binding, a_binding),
        created_at=NOW,
    )
    publication = ProductFamilyAuthoringPublication(
        operation=_operation(draft=draft),
        family=_family(),
        version=draft,
        geometry_bindings=draft.geometry_bindings,
        assets=_formal_assets(),
    )

    bundle = publication.to_bundle(version_no=1)

    assert tuple(item.binding_key for item in bundle.geometry_bindings) == (
        "a_geometry",
        "z_geometry",
    )


def test_commit_result_matches_the_published_operation_exact_version() -> None:
    draft = _draft()
    operation = _operation(draft=draft)
    publication = ProductFamilyAuthoringPublication(
        operation=operation,
        family=_family(),
        version=draft,
        geometry_bindings=draft.geometry_bindings,
        assets=_formal_assets(),
    )
    published = PublishedCriticalOperation(
        operation=operation,
        catalog_entry=DataCatalogEntry(
            catalog_entry_id="catalog-1",
            owner_id="owner-1",
            tenant_id="tenant-1",
            task_id="task-1",
            run_id="run-1",
            resource_kind=CatalogResourceKind.OPERATION,
            alias="op_001",
            ordinal=1,
            catalog_revision=1,
            operation_id=operation.operation_id,
            created_at=NOW,
        ),
    )
    result = AuthoringPublicationCommitResult(
        operation=published,
        family_version=publication.to_bundle(version_no=1),
    )
    assert result.family_version.family.family_id == operation.target_product_family_id
