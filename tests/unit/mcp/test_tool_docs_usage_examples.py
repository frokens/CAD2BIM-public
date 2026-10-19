from __future__ import annotations

import hashlib
import inspect
import json
import sys
from collections.abc import Mapping, Sequence
from unittest.mock import Mock

import ifcopenshell
import pyarrow as pa
import pytest
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.data_query_python import PythonArrowQueryWorkerRequest
from CAD2BIM_backend.domain.data_transform import (
    DataTransformWorkerParameter,
    DataTransformWorkerPolicy,
    DataTransformWorkerRequest,
    PythonArrowWorkerOutputDeclaration,
    PythonArrowWorkerPolicy,
    PythonArrowWorkerRequest,
)
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.geometry_runtime import (
    GeometryQueryRequest,
    GeometryValidateFunctionRequest,
)
from CAD2BIM_backend.application.product_realization_input_service import (
    normalize_product_realization_invocation,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationAuthorProposal,
    ProductRealizationInvocation,
    ProductRealizationRequest,
    ResolvedModelingFunctionBinding,
)
from CAD2BIM_backend.infrastructure.data_transform import SubprocessDuckDBDataTransformWorker
from CAD2BIM_backend.infrastructure.data_transform.python_arrow_worker import (
    SubprocessPythonArrowDataTransformWorker,
)
from CAD2BIM_backend.infrastructure.geometry_runtime.adapter import OcpCadQueryGeometryRuntime
from CAD2BIM_backend.infrastructure.geometry_runtime.build_context import GeometryBuildContext
from CAD2BIM_backend.infrastructure.ifc_query import IfcOpenShellQueryWorker
from CAD2BIM_backend.infrastructure.product_realization_runtime import (
    OcpCadQueryProductRealizationRuntime,
)
from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle
from CAD2BIM_backend.ports.data_transform import ArrowTransformInput
from CAD2BIM_backend.ports.ifc_query_worker import IfcQueryWorkerRequest
from CAD2BIM_mcp.direct.ifc.geometry.authoring_contracts import (
    validate_geometry_resource_write_input,
)
from CAD2BIM_mcp.direct.ifc.geometry.contracts import validate_geometry_query_input
from CAD2BIM_mcp.direct.ifc.query.contracts import validate_ifc_query_input
from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import (
    validate_product_realization_preview_provider_input,
)
from CAD2BIM_mcp.direct.ifc.product_family.write_contracts import (
    validate_product_family_write_provider_input,
)
from CAD2BIM_mcp.direct.ifc.product_family.contracts import (
    validate_product_family_list_input,
)
from CAD2BIM_mcp.direct.tool_support import ProviderJsonDocumentError, register_provider_document
from CAD2BIM_mcp.direct.query.python_inputs import validate_direct_data_query_python_input
from CAD2BIM_mcp.direct.query.sql_inputs import validate_direct_data_query_sql_input
from CAD2BIM_mcp.direct.server import create_direct_mcp
from CAD2BIM_mcp.tool_docs.common import COMMON_TYPES_RESOURCE_URI, COMMON_USAGE_RESOURCE_URI, common_usage_document
from tests.unit.mcp.test_provider_output_compaction import (
    _expand_shared_input,
    _read_contract, _read_shared_contracts, _contract_registry, _restore_shared_schema,
)
from CAD2BIM_mcp.direct.transform.inputs import validate_direct_data_transform_create_input
from CAD2BIM_mcp.tool_docs.data_query import (
    DATA_QUERY_PYTHON_MINIMAL_FRESH_EXAMPLE,
    DATA_QUERY_PYTHON_FRESH_EXAMPLE,
    DATA_QUERY_PYTHON_SOURCE_EXAMPLE,
    DATA_QUERY_SQL_FRESH_EXAMPLE,
    DATA_QUERY_SQL_SOURCE_EXAMPLE,
)
from CAD2BIM_mcp.tool_docs.data_transform import (
    DATA_TRANSFORM_DUCKDB_CREATE_EXAMPLE,
    DATA_TRANSFORM_DUCKDB_MINIMAL_CREATE_EXAMPLE,
    DATA_TRANSFORM_DUCKDB_SOURCE_EXAMPLE,
    DATA_TRANSFORM_PYTHON_CREATE_EXAMPLE,
    DATA_TRANSFORM_PYTHON_MINIMAL_CREATE_EXAMPLE,
    DATA_TRANSFORM_PYTHON_SOURCE_EXAMPLE,
)
from CAD2BIM_mcp.tool_docs.ifc_geometry import (
    GEOMETRY_AUTHORING_PROGRAM_USAGE,
    GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE,
    GEOMETRY_MODELING_FUNCTION_PARAMETER_SCHEMA_EXAMPLE,
    GEOMETRY_MODELING_FUNCTION_SOURCE_EXAMPLE,
    GEOMETRY_MODELING_FUNCTION_VALIDATION_SAMPLE_EXAMPLE,
    GEOMETRY_QUERY_FRESH_EXAMPLE,
    GEOMETRY_QUERY_SECTION_FRESH_EXAMPLE,
    GEOMETRY_QUERY_SECTION_SOURCE_EXAMPLE,
    GEOMETRY_QUERY_SOURCE_EXAMPLE,
)
from CAD2BIM_mcp.tool_docs.ifc_product_family import (
    PRODUCT_FAMILY_GEOMETRY_CREATE_EXAMPLE,
    PRODUCT_FAMILY_GEOMETRY_PARAMETER_SCHEMA_EXAMPLE,
    PRODUCT_FAMILY_GEOMETRY_SOURCE_EXAMPLE,
    PRODUCT_FAMILY_GEOMETRY_VALIDATION_SAMPLE_EXAMPLE,
    PRODUCT_FAMILY_CREATE_EXAMPLE,
    PRODUCT_FAMILY_PREVIEW_SCENARIOS_EXAMPLE,
    PRODUCT_FAMILY_REALIZE_SOURCE_EXAMPLE,
    PRODUCT_FAMILY_SOURCE_CHANGES_EXAMPLE,
    PRODUCT_FAMILY_LIST_CURSOR_EXAMPLE,
    PRODUCT_FAMILY_LIST_FIRST_EXAMPLE,
    PRODUCT_REALIZATION_SELF_GEOMETRY_CONDITION_EXAMPLE,
)
from CAD2BIM_mcp.tool_docs.ifc_query import (
    IFC_QUERY_PROGRAM_USAGE,
    IFC_QUERY_FRESH_EXAMPLE,
    IFC_QUERY_SOURCE_EXAMPLE,
)
from CAD2BIM_mcp.tool_docs.preview import FINITE_VALIDATION_GUIDANCE


def _table_input() -> ArrowTransformInput:
    return ArrowTransformInput(
        "records",
        pa.table(
            {
                "entity_key": ["entity_a", "entity_b", "entity_c"],
                "length_m": [0.25, 0.5, 1.0],
            }
        ).to_reader(),
    )


def test_geometry_family_write_preview_and_guides_share_finite_validation_boundary() -> None:
    from CAD2BIM_mcp.tool_docs.ifc_geometry import (
        IFC_GEOMETRY_PREVIEW_DESCRIPTION,
    )
    from CAD2BIM_mcp.tool_docs.ifc_product_family import (
        IFC_PRODUCT_REALIZATION_PREVIEW_DESCRIPTION,
    )
    from CAD2BIM_mcp.runner.tool_projection import runner_tool_description

    assert "not the full declared support domain" in FINITE_VALIDATION_GUIDANCE
    assert "technical failures are not business rejections" in FINITE_VALIDATION_GUIDANCE
    assert "BUILT or SOLVED alone does not prove material" in FINITE_VALIDATION_GUIDANCE
    for published_text in (
        IFC_GEOMETRY_PREVIEW_DESCRIPTION,
        runner_tool_description("ifc_geometry_preview", IFC_GEOMETRY_PREVIEW_DESCRIPTION),
        IFC_PRODUCT_REALIZATION_PREVIEW_DESCRIPTION,
    ):
        assert FINITE_VALIDATION_GUIDANCE not in published_text
        assert "cad2bim://contracts/common/authoring" in published_text


def test_complete_usage_examples_parse_through_the_public_direct_contracts() -> None:
    from CAD2BIM_mcp.tool_docs.data_transform import DATA_DERIVE_IFC_CREATE_EXAMPLE
    validate_direct_data_transform_create_input(DATA_DERIVE_IFC_CREATE_EXAMPLE)
    cases = (
        (GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE, validate_geometry_resource_write_input),
        (
            PRODUCT_FAMILY_GEOMETRY_CREATE_EXAMPLE,
            validate_geometry_resource_write_input,
        ),
        (PRODUCT_FAMILY_CREATE_EXAMPLE, validate_product_family_write_provider_input),
        (DATA_TRANSFORM_DUCKDB_CREATE_EXAMPLE, validate_direct_data_transform_create_input),
        (DATA_TRANSFORM_PYTHON_CREATE_EXAMPLE, validate_direct_data_transform_create_input),
        (DATA_TRANSFORM_DUCKDB_MINIMAL_CREATE_EXAMPLE, validate_direct_data_transform_create_input),
        (DATA_TRANSFORM_PYTHON_MINIMAL_CREATE_EXAMPLE, validate_direct_data_transform_create_input),
        (DATA_QUERY_SQL_FRESH_EXAMPLE, validate_direct_data_query_sql_input),
        (DATA_QUERY_PYTHON_FRESH_EXAMPLE, validate_direct_data_query_python_input),
        (DATA_QUERY_PYTHON_MINIMAL_FRESH_EXAMPLE, validate_direct_data_query_python_input),
        (GEOMETRY_QUERY_FRESH_EXAMPLE, validate_geometry_query_input),
        (GEOMETRY_QUERY_SECTION_FRESH_EXAMPLE, validate_geometry_query_input),
        (PRODUCT_FAMILY_LIST_FIRST_EXAMPLE, validate_product_family_list_input),
        (PRODUCT_FAMILY_LIST_CURSOR_EXAMPLE, validate_product_family_list_input),
        (IFC_QUERY_FRESH_EXAMPLE, validate_ifc_query_input),
    )
    for source, validator in cases:
        validator(json.loads(source))

    with pytest.raises(ValidationError):
        validate_product_family_list_input({"direct_session_alias": "direct_001", "mode": "first"})


def test_family_tutorial_requests_parse_by_binding_only_published_placeholders():
    from CAD2BIM_mcp.tool_docs.ifc_product_family import (
        PRODUCT_FAMILY_WRITE_TUTORIAL, PRODUCT_REALIZATION_PREVIEW_TUTORIAL,
    )
    from CAD2BIM_mcp.direct.ifc.authoring_publish import DirectAuthoringPublishInput
    from CAD2BIM_mcp.direct.ifc.preview_cases import parse_cases, parse_render
    from CAD2BIM_backend.domain.product_family_authoring import FamilyDraftDefinition
    from CAD2BIM_backend.domain.preview_cases import revise_preview_cases
    replacements = {"${DIRECT_SESSION_ALIAS}": "direct_001", "${DISPLAY_NAME}": "Fitting", "${PURPOSE}": "Inspect fitting", "${USAGE}": "Use declared dimensions.", "${CURRENT_FAMILY_VERSION_ALIAS}": "cur.example.family.version", "${DRAFT_REF}": "fd_" + "a" * 22, "${UPDATED_USAGE}": "Updated usage.", "${PREVIEW_OPERATION_ALIAS}": "op_001", "${SINGLE_OPERATION_ALIAS}": "op_002", "${SINGLE_INSPECTION_REF}": "cur.example.inspection", "${CHECKED_OPERATION_ALIAS}": "op_003"}
    validators = {
        "create": validate_product_family_write_provider_input,
        "update": validate_product_family_write_provider_input,
        "create_inline": validate_product_family_write_provider_input,
        "update_binding": validate_product_family_write_provider_input,
        "preview_single": validate_product_realization_preview_provider_input,
        "preview_saved_inputs": validate_product_realization_preview_provider_input,
        "preview_saved_observation": validate_product_realization_preview_provider_input,
        "publish": DirectAuthoringPublishInput.model_validate,
    }
    examples = {**PRODUCT_FAMILY_WRITE_TUTORIAL, **PRODUCT_REALIZATION_PREVIEW_TUTORIAL}
    requests = {}
    replacements["${CURRENT_GEOMETRY_VERSION_ALIAS}"] = "cur.example.geometry.version"
    for key, validator in validators.items():
        payload = json.dumps(examples[key])
        for placeholder, value in replacements.items():
            payload = payload.replace(placeholder, value)
        request = requests[key] = json.loads(payload)
        validator(request)
        if request.get("mode") == "cases":
            parse_cases(request["cases_json"], case_kind="family")
            parse_render(request.get("render_json"))
    # Reusing the single must preserve its complete input, not the tutorial's
    # separate normal case that also requests external conditions.
    single = requests["preview_single"]
    invocation = json.loads(single["invocation_json"])
    invocation["realization_kind"] = single["attachment_kind"]
    base = requests["preview_saved_observation"]
    cases_document = json.loads(base["cases_json"])
    alternative = json.dumps(examples["alternative_reuse"])
    for placeholder, value in replacements.items():
        alternative = alternative.replace(placeholder, value)
    variants = [base]
    cases_document["items"][0]["reuse"] = json.loads(alternative)
    variants.append({**base, "cases_json": json.dumps(cases_document)})
    cases_document["items"][0].pop("reuse")
    variants.append({**base, "cases_json": json.dumps(cases_document)})
    for request in variants:
        validate_product_realization_preview_provider_input(request)
        cases = parse_cases(request["cases_json"], case_kind="family")
        assert cases.items[0].invocation == ProductRealizationInvocation.model_validate(invocation)
        assert cases.items[0].expectation.expected_status.value == "SOLVED"
        assert requests["publish"]["preview_examples"][0]["case_keys"] == [case.case_key for case in cases.items]
        assert all(case.expectation is not None for case in cases.items)
        assert request["nominal_scenario_key"] in {case.case_key for case in cases.items}
    # Explicit reuse changes preserve all inputs and expectations while selecting
    # the current report, instead of inheriting an older nested reuse reference.
    inherited = parse_cases(base["cases_json"], case_kind="family")
    changes = parse_cases(requests["preview_saved_inputs"]["cases_json"], case_kind="family")
    revised = revise_preview_cases(inherited.items, changes.changes, operation_alias=changes.operation_alias)
    assert [case.case_key for case in revised] == ["normal", "standard_nominal", "standard_placed"]
    for original, case in zip(inherited.items, revised, strict=True):
        assert case.invocation == original.invocation and case.expectation == original.expectation
        assert case.reuse.operation_alias == "op_001" and case.reuse.case_key == case.case_key
    validate_product_realization_preview_provider_input({**single, "attachment_kind": "standard_model"})
    parse_render(json.dumps({"mode": "selected", "case_keys": ["standard_nominal"]}))
    assert requests["publish"]["preview_examples"][0]["operation_alias"] != "op_002"


def test_geometry_tutorial_saved_cases_reuse_original_input_and_expectation():
    from CAD2BIM_mcp.tool_docs.ifc_geometry import GEOMETRY_PREVIEW_TUTORIAL
    from CAD2BIM_mcp.direct.ifc.geometry.contracts import validate_geometry_preview_input
    from CAD2BIM_mcp.direct.ifc.authoring_publish import DirectAuthoringPublishInput
    from CAD2BIM_mcp.direct.ifc.preview_cases import parse_cases
    from CAD2BIM_backend.domain.preview_cases import revise_preview_cases
    examples = GEOMETRY_PREVIEW_TUTORIAL
    for key in ("single", "cases", "saved_cases"):
        validate_geometry_preview_input(examples[key])
    DirectAuthoringPublishInput.model_validate(examples["publish"])
    original = parse_cases(examples["cases"]["cases_json"], case_kind="geometry")
    saved = parse_cases(examples["saved_cases"]["cases_json"], case_kind="geometry")
    revised, = revise_preview_cases(original.items, saved.changes, operation_alias=saved.operation_alias)
    assert revised.parameters == original.items[0].parameters
    assert revised.expectation == original.items[0].expectation and revised.expectation.expected_status == "BUILT"
    assert revised.reuse.operation_alias == "op_002" and revised.reuse.case_key == "nominal"


def test_product_family_public_example_preserves_structured_values_and_external_condition() -> None:
    value = validate_product_family_write_provider_input(json.loads(PRODUCT_FAMILY_CREATE_EXAMPLE))
    scenario = ProductRealizationInvocation.model_validate(PRODUCT_FAMILY_PREVIEW_SCENARIOS_EXAMPLE[0]["invocation"])
    from CAD2BIM_backend.domain.product_family_authoring import FamilyDraftDefinition
    family = FamilyDraftDefinition.model_validate(json.loads(value.source.definition_json)["value"]).family_contract

    assert scenario.public_parameters["segment_lengths_m"] == [0.1, 0.2]
    assert scenario.public_parameters["section"] == {"inner_diameter_m": 0.04}
    assert isinstance(scenario.public_parameters["segment_lengths_m"], list)
    assert isinstance(scenario.public_parameters["section"], dict)
    assert len(scenario.external_constraints) == 1
    external = scenario.external_constraints[0]
    requirement = next(
        item for item in family.interfaces if item.interface_key == "outlet"
    ).external_constraint
    assert requirement is not None
    assert requirement.required is False
    assert requirement.allowed_kinds == ("fixed_point",)
    assert requirement.allowed_directions == ("opposed",)
    assert requirement.profile_required is True
    assert external.interface_key == "outlet"
    assert external.condition.kind == "fixed_point"
    assert external.condition.point_m == (0, 0, 0.3)
    assert external.condition.direction_relation == "opposed"
    assert external.condition.outer_diameter_m == 0.05

    from CAD2BIM_mcp.tool_docs.ifc_product_family import PRODUCT_REALIZATION_PREVIEW_TUTORIAL
    examples = PRODUCT_REALIZATION_PREVIEW_TUTORIAL["input_purpose_examples"]
    point_invocation = ProductRealizationInvocation.model_validate({"product_origin": [0, 0, 0], "length_unit": {},
        "external_constraints": [examples["pure_point_target"]]})
    assert not hasattr(point_invocation.external_constraints[0].condition, "axis")
    computation = examples["computation_only"]
    object_parameter = type(family.public_parameters[1]).model_validate(computation["parameter_definition"])
    assert object_parameter.properties[0].unit == "metre"
    assert computation["external_constraints"] == []


def test_product_family_numeric_source_example_returns_current_proposal_dto() -> None:
    namespace: dict[str, object] = {}
    exec(
        compile(PRODUCT_FAMILY_REALIZE_SOURCE_EXAMPLE, "<product-family-example>", "exec"),
        namespace,
    )

    class Context:
        @staticmethod
        def param(name: str):
            return {
                "segment_lengths_m": [0.1, 0.2],
                "section": {"inner_diameter_m": 0.04},
            }[name]

        @staticmethod
        def external_constraints():
            return (PRODUCT_FAMILY_PREVIEW_SCENARIOS_EXAMPLE[0]["invocation"]["external_constraints"][0],)

    adapter = TypeAdapter(ProductRealizationAuthorProposal)
    for entry_name in ("realize", "standard_model"):
        result = namespace[entry_name](Context())
        proposal = adapter.validate_python(result)
        assert tuple(item.component_key for item in proposal.components) == ("body", "tail")
        assert tuple(item.fragment_key for item in proposal.fragments) == (
            "body_shape",
            "tail_shape",
        )
        assert all("geometry_parameters" not in type(item).model_fields for item in proposal.fragments)
        assert all(item.local_transform is not None for item in proposal.fragments)
        assert all(item.uniform_scale == 1.0 for item in proposal.fragments)

    for retired in (
        "ctx.instantiate",
        "ctx.datum_transform",
        "root_world_transform",
        "instance_handle",
        "installation_anchor",
    ):
        assert retired not in PRODUCT_FAMILY_REALIZE_SOURCE_EXAMPLE


@pytest.mark.parametrize("parameter_key", ["segment_lengths_m", "section"])
@pytest.mark.parametrize("invalid_transport", ["value_json", "stringified_value"])
def test_product_family_public_example_rejects_incorrect_structured_value_transport(parameter_key, invalid_transport):
    from CAD2BIM_backend.domain.product_family_authoring import FamilyDraftDefinition
    family = FamilyDraftDefinition.model_validate(json.loads(json.loads(PRODUCT_FAMILY_CREATE_EXAMPLE)["source"]["definition_json"])["value"]).family_contract
    scenario = json.loads(json.dumps(PRODUCT_FAMILY_PREVIEW_SCENARIOS_EXAMPLE[0]["invocation"]))
    value = scenario["public_parameters"][parameter_key]
    scenario["public_parameters"][parameter_key] = {"value_json": json.dumps(value)} if invalid_transport == "value_json" else json.dumps(value)
    with pytest.raises(ValueError):
        normalize_product_realization_invocation(family, ProductRealizationInvocation.model_validate(scenario))


def test_product_family_public_source_change_example_parses_for_draft_update():
    parsed = validate_product_family_write_provider_input({"direct_session_alias":"direct_001", "mode":"update", "draft_ref":"fd_" + "a" * 22, "expected_edit_token":1, "source_changes_json":PRODUCT_FAMILY_SOURCE_CHANGES_EXAMPLE})
    change = parsed.to_domain_declaration()["source_changes"][0]
    assert change["path"] == "main.py"
    assert change["edits"][0] == {"old_text":"BROKEN_TOKEN", "new_text":"WORKING_TOKEN"}


@pytest.mark.parametrize("mutation", ["flat_replacement", "missing_source_path", "missing_old_text", "schema_marker"])
def test_product_family_public_source_change_example_rejects_invalid_literal_edit(mutation):
    document = json.loads(PRODUCT_FAMILY_SOURCE_CHANGES_EXAMPLE)
    change = document["changes"][0]
    if mutation == "flat_replacement":
        change.update(change.pop("replacements")[0])
    elif mutation == "missing_source_path":
        del change["source_path"]
    elif mutation == "missing_old_text":
        del change["replacements"][0]["old_text"]
    else:
        document["schema"] = "retired-marker"
    with pytest.raises(ProviderJsonDocumentError):
        validate_product_family_write_provider_input({"direct_session_alias":"direct_001", "mode":"update", "draft_ref":"fd_" + "a" * 22, "expected_edit_token":1, "source_changes_json":json.dumps(document)})


def test_product_realization_self_geometry_condition_example_is_provider_valid() -> None:
    value = validate_product_realization_preview_provider_input(
        {
            "direct_session_alias": "direct_001",
            "mode": "single",
            "attachment_kind": "self_geometry",
            "source": {"source_kind":"published", "family_version_alias":"cur.example.family.version"},
            "invocation_json": PRODUCT_REALIZATION_SELF_GEOMETRY_CONDITION_EXAMPLE,
        }
    )
    condition = value.external_constraints[0].condition
    assert condition.kind == "fixed_point"
    assert condition.direction_relation == "opposed"
    assert condition.axis == (0, 0, -1)
    assert condition.outer_diameter_m == 0.05
    assert condition.reference_direction is None


def test_geometry_define_interface_public_example_matches_keyword_only_runtime_abi() -> None:
    signature = inspect.signature(GeometryBuildContext.define_interface)
    assert tuple(signature.parameters) == (
        "self",
        "key",
        "origin_m",
        "axis",
        "reference_direction",
        "profile",
    )
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in tuple(signature.parameters.values())[1:]
    )


@pytest.mark.parametrize("mutation", ["schema_marker", "flat_binding", "connected", "attachments", "legacy_anchor", "standard_subset", "program_frame", "nested_extra", "missing_sources"])
def test_family_example_rejects_obsolete_contract_inputs(mutation):
    payload = json.loads(PRODUCT_FAMILY_CREATE_EXAMPLE)
    document = json.loads(payload["source"]["definition_json"])
    definition = document["value"]
    if mutation == "schema_marker":
        definition["family_contract"]["schema"] = "retired-marker"
    elif mutation == "flat_binding":
        fragment = definition["family_contract"]["geometry_fragments"][0]
        option, = fragment.pop("binding_options")
        fragment.update(option)
    elif mutation == "connected":
        definition["validation_scenarios"] = [{"realization_kind":"connected"}]
    elif mutation == "attachments":
        definition["validation_scenarios"] = [{"attachments":[]}]
    elif mutation == "standard_subset":
        definition["family_contract"]["standard_model"]["fragment_keys"] = ["body_shape"]
    elif mutation == "program_frame":
        definition["family_contract"]["interfaces"][0]["source"] = {"source_kind": "program_frame"}
    elif mutation == "nested_extra":
        definition["family_contract"]["geometry_fragments"][0]["binding_options"][0]["parameter_sources"]["overrides"]["length_m"]["unexpected"] = True
    elif mutation == "missing_sources":
        del definition["family_contract"]["geometry_fragments"][0]["binding_options"][0]["parameter_sources"]
    else:
        definition["family_contract"]["installation_anchor"] = {
            "source_kind": "model_interface",
            "fragment_key": "body_shape",
            "model_interface_key": "datum",
        }
    payload["source"]["definition_json"] = json.dumps(document)
    with pytest.raises(ProviderJsonDocumentError):
        validate_product_family_write_provider_input(payload)


def test_family_example_uses_only_business_structure():
    payload = json.loads(PRODUCT_FAMILY_CREATE_EXAMPLE)
    document = json.loads(payload["source"]["definition_json"])
    definition = document["value"]
    assert "schema" not in definition["family_contract"]
    payload["source"]["definition_json"] = json.dumps(document)
    declaration = validate_product_family_write_provider_input(payload).to_domain_declaration()
    from CAD2BIM_backend.domain.product_family import ProductFamilyContract
    contract = ProductFamilyContract.model_validate(declaration["source"]["definition"]["value"]["family_contract"])
    assert contract.product_structure_kind == "semantic_assembly"
    assert "schema" not in contract.model_dump()


def test_current_registration_sample_requests_keep_detailed_and_sphere_separate():
    from tests.scenarios.engineering_product_families.registration import candidate_requests

    manifest = json.loads(candidate_requests.CONNECTOR.manifest_path.read_text())
    descriptors = {item["binding_key"]: item for item in manifest["geometry_bindings"]}
    assert set(descriptors) == {"connector_body_geometry", "sphere_geometry"}
    requests = {key: candidate_requests.geometry_request(
                    item, candidate_requests.CONNECTOR, direct_session_alias="direct_001")
                for key, item in descriptors.items()}
    for request in requests.values():
        validate_geometry_resource_write_input(request)
    preview = candidate_requests.geometry_preview_request(descriptors["connector_body_geometry"],
        direct_session_alias="direct_001", draft_ref="returned-draft-ref", retain=True)
    detailed = [case["parameters"] for case in json.loads(preview["cases_json"])["items"]]
    assert len(detailed) == 4
    assert all(item["shape"]["mode"] in {"straight", "curved"} for item in detailed)
    sphere = requests["sphere_geometry"]["source"]
    assert sphere["parameter_schema_json"] != requests["connector_body_geometry"]["source"]["parameter_schema_json"]
    assert sphere["source_files"] != requests["connector_body_geometry"]["source"]["source_files"]


@pytest.mark.asyncio
async def test_current_family_guide_executes_actual_geometry_and_complete_standard_model(tmp_path):
    from CAD2BIM_mcp.tool_docs.ifc_product_family import PRODUCT_FAMILY_GEOMETRY_INTERFACES_SOURCE_EXAMPLE
    source = pack_source_bundle({"main.py": PRODUCT_FAMILY_GEOMETRY_SOURCE_EXAMPLE.encode(),
        "interfaces.py": PRODUCT_FAMILY_GEOMETRY_INTERFACES_SOURCE_EXAMPLE.encode()})
    geometry_runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "geometry")
    try:
        geometry = await geometry_runtime.validate_function(GeometryValidateFunctionRequest(
            source_bundle=source,
            parameter_schema=PRODUCT_FAMILY_GEOMETRY_PARAMETER_SCHEMA_EXAMPLE,
            sample_parameters=(PRODUCT_FAMILY_GEOMETRY_VALIDATION_SAMPLE_EXAMPLE,),
        ))
    finally:
        await geometry_runtime.close()
    assert geometry.sample_geometry.summary.topology.solid_count == 1
    assert {x.interface_key for x in geometry.sample_executions[0].interfaces} == {"datum", "inlet", "outlet"}
    input_value = validate_product_family_write_provider_input(json.loads(PRODUCT_FAMILY_CREATE_EXAMPLE))
    from CAD2BIM_backend.domain.product_family_authoring import FamilyDraftDefinition
    authoring_family = FamilyDraftDefinition.model_validate(json.loads(input_value.source.definition_json)["value"]).family_contract
    from CAD2BIM_backend.application.product_realization_input_service import ProductRealizationInputService
    authoring_family = ProductRealizationInputService.normalize_contract_defaults(authoring_family,
        {"body_geometry": PRODUCT_FAMILY_GEOMETRY_PARAMETER_SCHEMA_EXAMPLE})
    scenarios = tuple(
        normalize_product_realization_invocation(authoring_family, ProductRealizationInvocation.model_validate(item["invocation"]))
        for item in PRODUCT_FAMILY_PREVIEW_SCENARIOS_EXAMPLE
    )
    family = authoring_family
    program = pack_source_bundle({"main.py": PRODUCT_FAMILY_REALIZE_SOURCE_EXAMPLE.encode()})
    binding = ResolvedModelingFunctionBinding(
        resource_kind=GeometryResourceKind.MODELING_FUNCTION, binding_key="body_geometry",
        geometry_content_digest=hashlib.sha256(source).hexdigest(), source_bundle=source,
        source_bundle_sha256=hashlib.sha256(source).hexdigest(),
        parameter_schema=PRODUCT_FAMILY_GEOMETRY_PARAMETER_SCHEMA_EXAMPLE,
    )
    runtime = OcpCadQueryProductRealizationRuntime(capacity_root=tmp_path / "family")
    try:
        for scenario in scenarios:
            execution = await runtime.realize_geometry(ProductRealizationRequest(
                family_contract=family,
                family_contract_digest=sha256_digest(family), source_bundle=program,
                source_bundle_sha256=hashlib.sha256(program).hexdigest(), geometry_bindings=(binding,),
                invocation=scenario,
            ))
            assert execution.geometry is not None
            assert execution.geometry.summary.topology.solid_count == 2
            assert execution.realization.frame_check_status == "PASS"
            assert {x.interface_key for x in execution.realization.interface_project_world_frames} == {"inlet", "body_joint", "tail_joint", "outlet"}
            root = next(
                item.project_world_transform
                for item in execution.realization.component_project_world_transforms
                if item.component_key == "body"
            )
            assert root.translation_m == pytest.approx(scenario.product_origin)
            assert tuple(x.component_key for x in execution.realization.proposal.components) == ("body", "tail")
            assert tuple(x.fragment_key for x in execution.realization.proposal.fragments) == ("body_shape", "tail_shape")
            body_length, tail_length = scenario.public_parameters["segment_lengths_m"]
            diameter = scenario.external_constraints[0].condition.outer_diameter_m if scenario.external_constraints else 0.05
            internal = execution.realization.proposal.internal_parameters
            assert internal["body_length_m"] == pytest.approx(body_length)
            assert internal["tail_length_m"] == pytest.approx(tail_length)
            assert internal["diameter_m"] == pytest.approx(diameter)
            assert internal["inner_diameter_m"] == pytest.approx(scenario.public_parameters["section"]["inner_diameter_m"])
            tail = execution.realization.proposal.components[1]
            assert tail.local_transform.translation_m == pytest.approx((0, 0, body_length))
            assert all(fragment.local_transform is not None for fragment in execution.realization.proposal.fragments)
            assert all(fragment.uniform_scale == 1.0 for fragment in execution.realization.proposal.fragments)
            assert tuple(
                receipt.fragment_key for receipt in execution.realization.instance_receipts
            ) == ("body_shape", "tail_shape")
            assert all(check.evaluation.satisfied for check in execution.realization.frame_diagnostics)
    finally:
        await runtime.close()


def _description_values(value):
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "description" and isinstance(item, str):
                yield item
            yield from _description_values(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _description_values(item)


@pytest.mark.asyncio
async def test_family_json_contracts_are_discoverable_without_business_scope() -> None:
    from fastmcp.exceptions import NotFoundError
    from jsonschema import Draft202012Validator
    from CAD2BIM_mcp.tool_docs.ifc_product_family import (
        PRODUCT_FAMILY_DEFINITION_DOCUMENT_EXAMPLE,
        PRODUCT_FAMILY_WRITE_TUTORIAL,
        PRODUCT_REALIZATION_PREVIEW_TUTORIAL,
    )

    application = Mock()
    server = create_direct_mcp(application)
    common = await _read_shared_contracts(server)
    registry = _contract_registry(common)
    root = "cad2bim://contracts/ifc_product_family_write/"
    resources = {
        str(item.uri): item
        for item in await server.list_resources()
        if str(item.uri).startswith(root)
    }
    assert set(resources) == {
        root + "definition_json",
        root + "source_changes_json",
        root + "program_api",
        root + "tutorial",
    }
    documents = {}
    for uri, resource in resources.items():
        assert resource.mime_type == "application/json"
        contents = await server.read_resource(uri)
        assert len(contents.contents) == 1
        assert len(contents.contents[0].content.encode("utf-8")) <= 65_536
        documents[uri] = json.loads(contents.contents[0].content)
    definition_document = documents[root + "definition_json"]
    assert documents[root + "tutorial"] == PRODUCT_FAMILY_WRITE_TUTORIAL
    preview_root = "cad2bim://contracts/ifc_product_realization_preview/"
    assert await _read_contract(server, preview_root + "tutorial") == PRODUCT_REALIZATION_PREVIEW_TUTORIAL
    case_schema = await _read_contract(server, preview_root + "cases_json")
    example_items = case_schema["examples"][0]["items"]
    assert len(example_items) == 1
    assert example_items[0]["invocation"]["external_constraints"]
    assert tuple(definition_document) == ("create_definition_input_schema", "update_definition_input_schema")
    definition = definition_document["create_definition_input_schema"]
    validator = Draft202012Validator(definition, registry=registry)
    validator.validate({"action":"set", "value":PRODUCT_FAMILY_DEFINITION_DOCUMENT_EXAMPLE})
    assert not {"validation_scenarios", "nominal_scenario_key"} & set(definition.get("properties", {}))
    short_create = json.loads(json.dumps(PRODUCT_FAMILY_DEFINITION_DOCUMENT_EXAMPLE))
    short_create["geometry_bindings"][0]["source"]["resource_version_alias"] = "gv_" + "a" * 22
    validator.validate({"action":"set", "value":short_create})
    assert not validator.is_valid({"action":"set", "value":{**short_create, "validation_scenarios": []}})
    assert not validator.is_valid({"action":"set", "value":{**short_create, "unrecognized": True}})
    source_changes = documents[root + "source_changes_json"]
    source_changes_validator = Draft202012Validator(source_changes, registry=registry)
    assert source_changes_validator.is_valid(json.loads(PRODUCT_FAMILY_SOURCE_CHANGES_EXAMPLE))
    relation = _restore_shared_schema(definition, common)["$defs"]["InterfaceFrameRelation"]
    assert relation["properties"]["orientation_requirement"]["enum"]
    program = documents[root + "program_api"]
    returned = Draft202012Validator(program["return_schema"], registry=registry)
    # Inspect the actual public resource projection, including its shared defs.
    from tests.unit.mcp.test_direct_ifc_product_family_write_provider import _assert_provider_safe_schema
    definition_defs = _restore_shared_schema(definition, common)["$defs"]
    fragment_contract = definition_defs["ProductFamilyGeometryFragmentContract"]
    option_contract = definition_defs["ProductFamilyFragmentBindingOption"]
    placement_contract = _restore_shared_schema(program["return_schema"], common)["$defs"][
        "ProductRealizationAuthorFragmentPlacement"]
    for schema in (fragment_contract, option_contract, placement_contract):
        _assert_provider_safe_schema(schema)
        assert schema["additionalProperties"] is False
    assert "binding_options" in fragment_contract["required"]
    assert not {"binding_key", "parameter_sources", "uniform_scale_source"} & fragment_contract["properties"].keys()
    assert {"binding_key", "parameter_sources"} <= set(option_contract["required"])
    assert "binding_key" in placement_contract["required"]
    solved = {"status": "SOLVED", "components": [{"component_key": "body"}],
              "fragments": [{"fragment_key": "body_shape", "binding_key": "body_geometry"}]}
    assert returned.is_valid(solved)
    solved["fragments"][0]["geometry_parameters"] = {}
    assert not returned.is_valid(solved)
    del solved["fragments"][0]["geometry_parameters"]
    del solved["fragments"][0]["binding_key"]
    assert not returned.is_valid(solved)
    assert returned.is_valid({"status": "INFEASIBLE", "violations": [{"violation_key": "conflict", "message": "Conflicting requirements"}]})
    assert not returned.is_valid({"status": "INFEASIBLE", "violations": []})
    assert "ctx.external_constraints()" in program["usage"]
    assert "defaults to true: omitting required makes that condition mandatory" in program["usage"]
    assert "Set required=false when callers may omit it" in program["usage"]
    assert "every supplied condition is still independently checked" in program["usage"]
    assert "does not make the Program return INFEASIBLE" in program["usage"]
    assert "declared support, business, geometry, or assembly constraint" in program["usage"]
    assert "No general full-domain proof is required" in program["usage"]
    assert "proven physical conflict" not in program["usage"]
    # Verify the public resource carries the method and does not retain the
    # old unconditional classification of every bounded candidate miss.
    assert "predeclared deterministic rule" in program["usage"]
    assert "ordinary resources need no new mode/reason fields" in program["usage"]
    assert "does not automatically waive occupancy, installation or functional requirements" in program["usage"]
    assert "A bounded search miss or unresolved numerical capacity is AMBIGUOUS" not in program["usage"]
    # The static resource cannot be turned into a filesystem reader.
    with pytest.raises(NotFoundError, match="Unknown resource"):
        await server.read_resource(root + "../../private")
    assert not application.mock_calls

def _assert_contract_schema_references(schema):
    """Resolve only Schema positions, independently within each document root."""
    def walk(node):
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            ref = node["$ref"]
            assert ref.startswith("#/"), ref
            target = schema
            for token in ref[2:].split("/"):
                target = target[token.replace("~1", "/").replace("~0", "~")]
            assert isinstance(target, (dict, bool))
        for key in ("properties", "$defs", "patternProperties", "dependentSchemas"):
            for child in node.get(key, {}).values():
                walk(child)
        for key in ("items", "additionalProperties", "propertyNames", "contains"):
            walk(node.get(key))
        for key in ("oneOf", "anyOf", "allOf", "prefixItems"):
            for child in node.get(key, ()):
                walk(child)
    walk(schema)


@pytest.mark.asyncio
async def test_all_static_contract_resources_keep_real_navigation_json_and_reference_roots():
    from jsonschema import Draft202012Validator
    from CAD2BIM_mcp.tool_docs import data_transform as transform_docs
    from CAD2BIM_mcp.tool_docs import ifc_geometry as geometry_docs
    from CAD2BIM_mcp.tool_docs.ifc_product_family import (
        PRODUCT_FAMILY_WRITE_TUTORIAL, PRODUCT_REALIZATION_PREVIEW_TUTORIAL,
    )

    application = Mock()
    server = create_direct_mcp(application)
    common = await _read_shared_contracts(server)
    resources = await server.list_resources()
    tools = {tool.name: tool.to_mcp_tool() for tool in await server.list_tools()}
    documents = {}
    for resource in resources:
        uri = str(resource.uri)
        assert uri.startswith("cad2bim://contracts/")
        contents = await server.read_resource(uri)
        assert len(contents.contents) == 1 and resource.mime_type == "application/json"
        assert len(contents.contents[0].content.encode("utf-8")) <= 65_536
        body = documents[uri] = json.loads(contents.contents[0].content)
        assert isinstance(body, dict)
        roots = [body] if any(key in body for key in ("type", "properties", "$defs", "oneOf", "anyOf")) else [
            value for key, value in body.items() if key.endswith("_schema") and isinstance(value, dict)
        ]
        if isinstance(body.get("target_schemas"), dict):
            roots.extend(body["target_schemas"].values())
        for schema in roots:
            Draft202012Validator.check_schema(schema)
            _assert_contract_schema_references(_restore_shared_schema(schema, common))
    root = "cad2bim://contracts/"
    existing_contracts = {
        "ifc_product_family_write/definition_json",
        "ifc_product_family_write/source_changes_json",
        "ifc_product_family_write/program_api",
        "ifc_product_realization_preview/self_geometry",
        "ifc_product_realization_preview/standard_model",
        "ifc_product_realization_preview/result",
        "ifc_geometry_preview/result",
        "ifc_geometry_preview/cases_json",
        "ifc_geometry_preview/render_json",
        "ifc_product_realization_preview/cases_json",
        "ifc_product_realization_preview/render_json",
    }
    assert {root + suffix for suffix in existing_contracts} <= documents.keys()
    expected_resources = {root + suffix for suffix in existing_contracts} | {
        root + "ifc_product_family_write/tutorial",
        root + "ifc_product_realization_preview/tutorial",
        root + "ifc_geometry_preview/tutorial",
        root + "common/preview_types",
        root + "common/authoring",
        root + "common/preview",
        COMMON_TYPES_RESOURCE_URI,
        COMMON_USAGE_RESOURCE_URI,
    }
    for name, suffixes in {
        "data_derive": ("duckdb_sql", "python_arrow", "repair"),
        "ifc_geometry_resource_write": ("program_api",),
        "data_query_python": ("program_api",),
        "ifc_geometry_query_python": ("program_api",),
        "ifc_query_python": ("program_api",),
        "ifc_products_apply": ("input",),
        "ifc_products_edit": ("input",),
        "ifc_products_read": ("usage",),
    }.items():
        for suffix in suffixes:
            uri = f"{root}{name}/{suffix}"
            expected_resources.add(uri)
            assert uri in documents and uri in tools[name].description
    assert set(documents) == expected_resources
    from CAD2BIM_mcp.tool_docs.ifc_query import IFC_SEMANTIC_EXTRACTION_GUIDANCE
    assert IFC_SEMANTIC_EXTRACTION_GUIDANCE in documents[root + "ifc_query_python/program_api"]["usage"]
    assert documents[root + "data_derive/python_arrow"]["ifc_semantic_extraction"] == IFC_SEMANTIC_EXTRACTION_GUIDANCE
    from CAD2BIM_mcp.tool_docs.common import IFC_INSTALLATION_EVIDENCE_GUIDANCE
    assert IFC_INSTALLATION_EVIDENCE_GUIDANCE in documents[root + "ifc_query_python/program_api"]["usage"]
    assert documents[root + "data_derive/python_arrow"]["ifc_installation_evidence"] == IFC_INSTALLATION_EVIDENCE_GUIDANCE
    apply_guide = documents[root + "ifc_products_apply/input"]
    assert apply_guide["external_conditions"]["evidence"] == IFC_INSTALLATION_EVIDENCE_GUIDANCE
    assert "Cross-product fit" in apply_guide["report_review"]["ifc_checks"]
    assert "does not establish cross-product" in tools["ifc_products_apply"].description
    property_states = documents[root + "ifc_products_read/usage"]["property_states"]
    assert all(term in property_states for term in ("absent", "explicit null", "names-only", "instance/type", "units"))
    edit = documents[root + "ifc_products_edit/input"]
    assert all(term in edit["family_mapping"] for term in ("multi-Family", "old IFC class", "unchanged", "read fails", "before publishing"))
    assert all(column in edit["input_preflight"] for column in ("product_key", "target_global_id", "action", "family_version_ref"))
    assert documents[COMMON_USAGE_RESOURCE_URI] == common_usage_document()
    import re
    public_navigation = " ".join(
        [tool.description for tool in tools.values()]
        + [resource.description for resource in resources]
    )
    sections = set(re.findall(re.escape(COMMON_USAGE_RESOURCE_URI) + r"#([a-z_]+)", public_navigation))
    # Whole-page navigation is valid; each subsection need not be repeated in D.
    assert sections <= set(documents[COMMON_USAGE_RESOURCE_URI])
    assert set(documents[COMMON_USAGE_RESOURCE_URI]) == {"references", "version_usage", "support_scope"}
    assert all("#" not in uri for uri in documents)
    assert documents[root + "ifc_product_family_write/tutorial"] == PRODUCT_FAMILY_WRITE_TUTORIAL
    assert documents[root + "ifc_product_realization_preview/tutorial"] == PRODUCT_REALIZATION_PREVIEW_TUTORIAL
    python_query = documents[root + "data_query_python/program_api"]
    assert "tool_result_schema" not in python_query
    navigation = python_query["mcp_result_navigation"]
    assert navigation["registered_tool"] == "data_query_python"
    assert navigation["top_level_fields"] == list(tools["data_query_python"].outputSchema["properties"])
    assert "program_return" in python_query and "page.next_cursor" in navigation["discovery"]
    assert "Clients need not expose that API" in navigation["discovery"]
    assert set(navigation["result_fields"]) == set(navigation["top_level_fields"])
    assert "next_cursor=null" in navigation["result_fields"]["page"]
    assert documents[root + "data_derive/duckdb_sql"] == transform_docs.DATA_TRANSFORM_DUCKDB_DOCUMENT
    assert documents[root + "data_derive/python_arrow"] == transform_docs.DATA_TRANSFORM_PYTHON_DOCUMENT
    assert documents[root + "data_derive/repair"] == transform_docs.DATA_TRANSFORM_REPAIR_DOCUMENT
    geometry = documents[root + "ifc_geometry_resource_write/program_api"]
    assert set(geometry) == {"usage", "interface_schema", "return_contract", "execution_diagnostics", "examples"}
    assert geometry["usage"] == geometry_docs.GEOMETRY_AUTHORING_PROGRAM_USAGE
    assert "may be a nominal interface when the resource declares" in geometry["usage"]
    assert "actual openings, material contact, body clearance or later installation" in geometry["usage"]
    assert "allowed representations, their declared triggers" in documents[COMMON_USAGE_RESOURCE_URI]["version_usage"]
    assert geometry["return_contract"] == geometry_docs.GEOMETRY_BUILD_RETURN_CONTRACT
    assert "Write only saves source and declarations" in geometry["return_contract"]
    assert "results live only for the current write" not in geometry["return_contract"]
    assert "runtime_checked=false" in geometry["execution_diagnostics"]
    assert "single" in documents[root + "common/authoring"]["workflow"]
    assert "without" in documents[root + "common/authoring"]["workflow"]
    assert "reuse" in documents[root + "common/authoring"]["reuse"]
    assert "inherited cases not deleted or marked for reuse" in documents[root + "common/authoring"]["reuse"]
    assert "delete the other unneeded cases" in documents[root + "common/authoring"]["reuse"]
    public_request = geometry["examples"]["modeling_function_create"]
    assert public_request == json.loads(geometry_docs.GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE)
    validate_geometry_resource_write_input(public_request)
    assert not application.mock_calls

@pytest.mark.asyncio
async def test_static_contract_document_utf8_limit_rejects_without_truncating():
    from fastmcp import FastMCP

    server = FastMCP("static contract byte boundary")
    overhead = len(json.dumps({"text": ""}, ensure_ascii=False, separators=(",", ":")).encode())
    text_value = "a" * (65_536 - overhead)
    register_provider_document(server, tool_name="boundary", document_name="exact", description="Exact UTF-8 limit", document={"text": text_value})
    with pytest.raises(ValueError, match="65,536 bytes"):
        register_provider_document(server, tool_name="boundary", document_name="too_large", description="Rejected", document={"text": text_value + "中"})
    contents = await server.read_resource("cad2bim://contracts/boundary/exact")
    assert len(contents.contents[0].content.encode("utf-8")) == 65_536
    assert json.loads(contents.contents[0].content) == {"text": text_value}
    assert [str(resource.uri) for resource in await server.list_resources()] == ["cad2bim://contracts/boundary/exact"]

@pytest.mark.asyncio
async def test_tool_declarations_expose_current_usage_and_failure_boundaries() -> None:
    server = create_direct_mcp(Mock())
    tools = await server.list_tools()
    templates = await server.list_resource_templates()
    tool_payload = [
        tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in tools
    ]
    for tool in tool_payload:
        tool["inputSchema"] = _expand_shared_input(tool["inputSchema"])
    template_payload = [
        template.to_mcp_template().model_dump(mode="json", by_alias=True, exclude_none=True)
        for template in templates
    ]
    assert template_payload == []

    by_name = {tool["name"]: tool for tool in tool_payload}
    geometry_preview = by_name["ifc_geometry_preview"]["description"]
    family_preview = by_name["ifc_product_realization_preview"]["description"]
    geometry_write = by_name["ifc_geometry_resource_write"]["description"]
    family_write = by_name["ifc_product_family_write"]["description"]
    ifc_query = by_name["ifc_query_python"]["description"]
    code_list = by_name["code_list"]["description"]
    code_read = by_name["code_read"]["description"]
    assert all(mode in geometry_preview for mode in ("single", "cases", "inspection"))
    assert "without rebuilding" in geometry_preview
    assert all(mode in family_preview for mode in ("single", "cases", "inspection"))
    assert "retain=true" in family_preview and "retain=true" in geometry_preview
    common_usage = await _read_contract(server, COMMON_USAGE_RESOURCE_URI)
    geometry_authoring_text = geometry_write + " " + GEOMETRY_AUTHORING_PROGRAM_USAGE
    for write_description in (geometry_authoring_text, family_write):
        assert "draft" in write_description and "Preview" in write_description
    # Verify the published guide, without freezing a prose template or project workflow.
    assert common_usage == common_usage_document()
    ifc_query_text = ifc_query + " " + IFC_QUERY_PROGRAM_USAGE
    assert "Local dictionary and list index assignment or deletion is allowed" in ifc_query_text
    assert "IFC entity index write or deletion is rejected" in ifc_query_text
    assert "page_size defaults to 20" in code_list
    assert "integer from 1 through 50" in code_list
    assert "returned_count=0 and codes=[]" in code_list
    assert "only code_locator is accepted by code_read" in code_list
    assert "owner_alias" in code_read
    assert "are not code locators" in code_read

    # Discovery rejections do not terminate pagination or grant source access.
    family_list = by_name["ifc_product_family_list"]["description"]
    for description in (family_list, code_list):
        assert "PRODUCT_FAMILY_CONTRACT_INVALID" in description
        assert "rejections" in description and "last scanned row" in description
        assert "cannot be executed or read as source" in description
        assert "Technical failures" in description
    assert "match owner_alias to the exact Family version" in code_list
    catalog = by_name["data_package_list"]["description"]
    assert "different database or subject" in catalog
    assert "snapshot is not a live Package query" in catalog

    # Check the registered resource/field guidance, not a private copy of the prose.
    invocation_text = json.dumps(by_name["ifc_product_realization_preview"]["inputSchema"])
    assert "retained_direction" in invocation_text and "pipe_outward" in invocation_text
    assert "product interface axis" in invocation_text
    resources_by_uri = {str(resource.uri): resource for resource in await server.list_resources()}
    self_geometry = resources_by_uri["cad2bim://contracts/ifc_product_realization_preview/self_geometry"]
    assert "computation data" in self_geometry.description
    assert "independently asserts" in self_geometry.description
    preview = await _read_contract(server, "cad2bim://contracts/common/preview")
    assert "Unperformed entity or connectivity" in preview["preview_results"]

    forbidden = (
        "Codex-safe",
        "AIMessage",
        "ToolMessage",
        "M2b",
        "B3.05",
        "migration 0059",
        "Agent-scheduling",
        "cross-agent",
        "Checkpoint boundary",
        "Application Core",
        "new-source",
        "legacy ABI",
        "Provider JSON",
        "Provider object",
        "The Provider",
    )
    for description in _description_values(tool_payload + template_payload):
        assert not any(term in description for term in forbidden), description


@pytest.mark.asyncio
async def test_geometry_and_family_examples_execute_in_the_real_runtimes(tmp_path) -> None:
    from CAD2BIM_mcp.tool_docs.ifc_geometry import GEOMETRY_INTERFACE_SOURCE_EXAMPLE
    geometry_source = pack_source_bundle(
        {"main.py": GEOMETRY_MODELING_FUNCTION_SOURCE_EXAMPLE.encode("utf-8"),
         "interfaces.py": GEOMETRY_INTERFACE_SOURCE_EXAMPLE.encode("utf-8")}
    )
    geometry_runtime = OcpCadQueryGeometryRuntime(capacity_root=tmp_path / "geometry-capacity")
    try:
        geometry = await geometry_runtime.validate_function(
            GeometryValidateFunctionRequest(
                source_bundle=geometry_source,
                parameter_schema=GEOMETRY_MODELING_FUNCTION_PARAMETER_SCHEMA_EXAMPLE,
                sample_parameters=(GEOMETRY_MODELING_FUNCTION_VALIDATION_SAMPLE_EXAMPLE,),
            )
        )
        assert geometry.sample_geometry.summary.topology.solid_count == 1
        assert tuple(item.interface_key for item in geometry.sample_executions[0].interfaces) == (
            "inlet",
            "outlet",
        )
        geometry_query = await geometry_runtime.query(
            GeometryQueryRequest(
                geometry=geometry.sample_geometry,
                source_code=GEOMETRY_QUERY_SOURCE_EXAMPLE,
            )
        )
        section_query = await geometry_runtime.query(
            GeometryQueryRequest(
                geometry=geometry.sample_geometry,
                source_code=GEOMETRY_QUERY_SECTION_SOURCE_EXAMPLE,
            )
        )
    finally:
        await geometry_runtime.close()

    assert geometry_query.result["solid_count"] == 1
    assert geometry_query.result["xmin_m"] == pytest.approx(0.0, abs=1e-6)
    assert geometry_query.result["xmax_m"] == pytest.approx(0.8, abs=1e-6)
    assert section_query.result["section_edge_count"] == 4
    assert sorted(section_query.result["section_edge_lengths_m"]) == pytest.approx(
        [0.1, 0.1, 0.2, 0.2], abs=1e-6
    )

@pytest.mark.asyncio
async def test_data_transform_and_query_examples_execute_in_real_workers(tmp_path) -> None:
    parameter = (DataTransformWorkerParameter("minimum_length_m", 0.5),)
    duckdb = SubprocessDuckDBDataTransformWorker(
        policy=DataTransformWorkerPolicy(wall_seconds=10, cpu_seconds=20),
        python_executable=sys.executable,
        capacity_root=tmp_path / "duckdb-capacity",
    )
    try:
        (duckdb_transform,) = await duckdb.execute(
            DataTransformWorkerRequest(
                code=DATA_TRANSFORM_DUCKDB_SOURCE_EXAMPLE,
                parameters=parameter,
                result_mode="named_relations",
                output_names=("selected",),
            ),
            (_table_input(),),
        )
        (duckdb_query,) = await duckdb.execute(
            DataTransformWorkerRequest(
                code=DATA_QUERY_SQL_SOURCE_EXAMPLE,
                parameters=parameter,
            ),
            (_table_input(),),
        )
    finally:
        await duckdb.close()

    assert duckdb_transform.receipt.row_count == 2
    assert duckdb_query.receipt.row_count == 2

    python_worker = SubprocessPythonArrowDataTransformWorker(
        policy=PythonArrowWorkerPolicy(
            wall_seconds=20,
            total_call_seconds=40,
            execution_seconds=25,
            terminal_seconds=5,
            cleanup_seconds=5,
        ),
        python_executable=sys.executable,
        capacity_root=tmp_path / "python-capacity",
    )
    try:
        python_outputs = await python_worker.execute(
            PythonArrowWorkerRequest(
                code=DATA_TRANSFORM_PYTHON_SOURCE_EXAMPLE,
                parameters=parameter,
                outputs=(
                    PythonArrowWorkerOutputDeclaration("selected", "parquet"),
                    PythonArrowWorkerOutputDeclaration("summary", "json", "document"),
                ),
            ),
            (_table_input(),),
        )
        python_query = await python_worker.execute_query(
            PythonArrowQueryWorkerRequest(
                code=DATA_QUERY_PYTHON_SOURCE_EXAMPLE,
                parameters=parameter,
            ),
            (_table_input(),),
        )
    finally:
        await python_worker.close()

    assert [item.receipt.row_count for item in python_outputs] == [2, 1]
    assert python_query.receipt.row_count == 2


@pytest.mark.asyncio
async def test_ifc_query_example_executes_in_the_real_worker(tmp_path) -> None:
    model = ifcopenshell.file(schema="IFC4X3")
    model.create_entity(
        "IfcProject",
        GlobalId=ifcopenshell.guid.new(),
        Name="Example project",
    )
    ifc_bytes = model.to_string().encode("utf-8")
    worker = IfcOpenShellQueryWorker(
        python_executable=sys.executable,
        capacity_root=tmp_path / "ifc-query-capacity",
    )
    try:
        result = await worker.execute_query(
            IfcQueryWorkerRequest(
                code=IFC_QUERY_SOURCE_EXAMPLE,
                ifc_bytes=ifc_bytes,
                expected_ifc_sha256=hashlib.sha256(ifc_bytes).hexdigest(),
            )
        )
    finally:
        await worker.close()

    assert result.result == {
        "counts": {"IfcProject": 1},
        "first_project_name": "Example project",
    }
