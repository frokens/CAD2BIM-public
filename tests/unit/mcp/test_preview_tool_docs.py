from __future__ import annotations

import json

import pytest

from CAD2BIM_backend.domain.geometry_runtime import GeometryPreviewProfile
from CAD2BIM_mcp.direct.ifc.geometry.contracts import (
    DirectIfcGeometrySinglePreviewInput,
    decode_geometry_preview_profile_json,
    validate_geometry_preview_input,
)
from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import (
    DirectIfcProductRealizationSolveProviderInput,
    validate_product_realization_preview_provider_input,
    validate_product_realization_preview_input,
)
from CAD2BIM_mcp.direct.tool_support import ProviderJsonDocumentError
from CAD2BIM_mcp.tool_docs.ifc_product_family import (
    IFC_PRODUCT_REALIZATION_PREVIEW_DESCRIPTION,
    PRODUCT_REALIZATION_INVOCATION_JSON_FIELD_DESCRIPTION,
    PRODUCT_REALIZATION_PUBLIC_PARAMETERS_INVOCATION_JSON,
    PRODUCT_REALIZATION_SELF_GEOMETRY_INVOCATION_JSON,
)
from CAD2BIM_mcp.tool_docs.ifc_geometry import IFC_GEOMETRY_PREVIEW_DESCRIPTION
from CAD2BIM_mcp.tool_docs.preview import (
    GEOMETRY_PREVIEW_PROFILE_CONNECTION_AUTO_JSON,
    GEOMETRY_PREVIEW_PROFILE_CONNECTION_PIPES_JSON,
    GEOMETRY_PREVIEW_PROFILE_INTERFACES_ALL_JSON,
    GEOMETRY_PREVIEW_PROFILE_JSON_FIELD_DESCRIPTION,
    GEOMETRY_PREVIEW_PROFILE_MINIMAL_JSON,
    GEOMETRY_PREVIEW_PROFILE_TWO_PANEL_CLEAN_JSON,
    GEOMETRY_PREVIEW_PROFILE_TWO_PANEL_INTERFACES_TEMPLATE,
    PRODUCT_REALIZATION_PREVIEW_PROFILE_JSON_FIELD_DESCRIPTION,
)


def _validate_through_both_preview_providers(profile_json: str) -> None:
    domain_profile = GeometryPreviewProfile.model_validate_json(profile_json)
    geometry = validate_geometry_preview_input(
        {
            "direct_session_alias": "direct_001",
            "mode": "single",
            "source": {"source_kind": "published", "resource_version_alias": "cur.exact_geometry_version"},
            "preview_profile_json": profile_json,
        }
    )
    product = validate_product_realization_preview_provider_input(
        {
            "direct_session_alias": "direct_001",
            "mode": "single",
            "attachment_kind": "self_geometry",
            "source": {"source_kind": "published", "family_version_alias": "cur.exact_family_version"},
            "invocation_json": PRODUCT_REALIZATION_SELF_GEOMETRY_INVOCATION_JSON,
            "preview_profile_json": profile_json,
        }
    )

    assert decode_geometry_preview_profile_json(geometry.preview_profile_json) == domain_profile
    assert product.render_profile == domain_profile


@pytest.mark.parametrize(
    "profile_json",
    (
        GEOMETRY_PREVIEW_PROFILE_MINIMAL_JSON,
        GEOMETRY_PREVIEW_PROFILE_TWO_PANEL_CLEAN_JSON,
        GEOMETRY_PREVIEW_PROFILE_INTERFACES_ALL_JSON,
        GEOMETRY_PREVIEW_PROFILE_CONNECTION_AUTO_JSON,
        GEOMETRY_PREVIEW_PROFILE_CONNECTION_PIPES_JSON,
    ),
)
def test_shared_preview_examples_parse_as_domain_geometry_and_product_inputs(
    profile_json: str,
) -> None:
    _validate_through_both_preview_providers(profile_json)


def test_saved_observation_profile_is_a_native_object_and_diagnostic_example_is_complete():
    from pydantic import ValidationError
    from CAD2BIM_mcp.direct.ifc.geometry.contracts import validate_geometry_query_input
    from CAD2BIM_mcp.tool_docs.ifc_geometry import GEOMETRY_QUERY_DISPLAY_DIAGNOSTICS_EXAMPLE
    profile = json.loads(GEOMETRY_PREVIEW_PROFILE_MINIMAL_JSON)
    request = {"direct_session_alias": "direct_001", "mode": "inspection", "inspection_ref": "cur.inspection_exact",
        "selection": {"selection_kind": "whole"}, "coordinate_space": {"coordinate_space": "result"},
        "observation": {"observation_kind": "model", "render_profile": profile}}
    validated = validate_geometry_preview_input(request)
    assert validated.observation.render_profile == GeometryPreviewProfile.model_validate(profile)
    with pytest.raises(ValidationError):
        validate_geometry_preview_input({**request, "observation": {**request["observation"], "render_profile": json.dumps(profile)}})
    diagnostic = validate_geometry_query_input(GEOMETRY_QUERY_DISPLAY_DIAGNOSTICS_EXAMPLE)
    assert diagnostic.code == "def query(ctx):\n    return ctx.display_diagnostics()\n"
    assert diagnostic.target.selection.selection_kind == "whole"


def test_interface_focus_example_copies_exact_overlay_ids_from_current_response() -> None:
    current_response = {
        "interface_overlays": (
            {"overlay_id": "family:upstream"},
            {"overlay_id": "family:downstream"},
        )
    }
    first, second = (item["overlay_id"] for item in current_response["interface_overlays"])
    profile_json = GEOMETRY_PREVIEW_PROFILE_TWO_PANEL_INTERFACES_TEMPLATE.replace(
        "__FIRST_RESPONSE_OVERLAY_ID__", first
    ).replace("__SECOND_RESPONSE_OVERLAY_ID__", second)

    _validate_through_both_preview_providers(profile_json)
    profile = GeometryPreviewProfile.model_validate_json(profile_json)
    assert profile.presentation is not None
    interface_camera = profile.presentation.panels[0].camera
    assert interface_camera.camera_kind == "interface_focus"
    assert interface_camera.overlay_ids == (first, second)
    assert profile.presentation.overlay.focus.focus_kind == "interface_pair"
    assert profile.presentation.overlay.focus.overlay_ids == (first, second)


def test_geometry_temporary_interface_clear_remains_product_preview_forbidden() -> None:
    value = json.loads(GEOMETRY_PREVIEW_PROFILE_INTERFACES_ALL_JSON)
    value["presentation"]["overlay"]["temporary_interfaces"] = []
    profile_json = json.dumps(value, separators=(",", ":"), sort_keys=True)

    geometry = validate_geometry_preview_input(
        {
            "direct_session_alias": "direct_001",
            "mode": "single",
            "source": {"source_kind": "published", "resource_version_alias": "cur.exact_geometry_version"},
            "preview_profile_json": profile_json,
        }
    )
    geometry_profile = decode_geometry_preview_profile_json(geometry.preview_profile_json)
    assert geometry_profile is not None
    assert geometry_profile.presentation is not None
    assert geometry_profile.presentation.overlay.temporary_interfaces == ()

    with pytest.raises(ProviderJsonDocumentError, match="temporary_interfaces"):
        validate_product_realization_preview_provider_input(
            {
                "direct_session_alias": "direct_001",
                "mode": "single",
                "attachment_kind": "self_geometry",
                "source": {"source_kind": "published", "family_version_alias": "cur.exact_family_version"},
                "invocation_json": PRODUCT_REALIZATION_SELF_GEOMETRY_INVOCATION_JSON,
                "preview_profile_json": profile_json,
            }
        )




def test_documented_parameterized_family_invocation_is_complete() -> None:
    request = validate_product_realization_preview_provider_input(
        {
            "direct_session_alias": "direct_001",
            "mode": "single",
            "attachment_kind": "self_geometry",
            "source": {"source_kind": "published", "family_version_alias": "cur.exact_family_version"},
            "invocation_json": PRODUCT_REALIZATION_PUBLIC_PARAMETERS_INVOCATION_JSON,
        }
    )

    assert request.public_parameters == {
        "segment_lengths_m": [0.1, 0.2],
        "section": {"inner_diameter_m": 0.04},
    }
    assert request.product_origin == (0, 0, 0)
    assert request.length_unit.name == "METRE"
    assert request.length_unit.prefix is None


@pytest.mark.asyncio
async def test_family_case_budget_is_in_the_readable_resource_body() -> None:
    from unittest.mock import Mock
    from CAD2BIM_mcp.direct.server import create_direct_mcp
    from CAD2BIM_backend.domain.product_realization import PRODUCT_REALIZATION_CPU_SECONDS, PRODUCT_REALIZATION_WALL_SECONDS
    from tests.unit.mcp.test_provider_output_compaction import _read_contract
    document = await _read_contract(create_direct_mcp(Mock()),
        "cad2bim://contracts/ifc_product_realization_preview/cases_json")
    assert f"{PRODUCT_REALIZATION_CPU_SECONDS:g}-second CPU limit" in document["usage"]
    assert f"{PRODUCT_REALIZATION_WALL_SECONDS:g}-second wall limit" in document["usage"]
    assert "Geometry construction" in document["usage"]
    assert "across native threads" in document["usage"]
    assert "not_run" in document["usage"]


@pytest.mark.asyncio
async def test_preview_field_help_uses_the_shared_precise_profile_shape() -> None:
    from unittest.mock import Mock
    from CAD2BIM_mcp.direct.server import create_direct_mcp
    from CAD2BIM_mcp.tool_docs.common import COMMON_PREVIEW_RESOURCE_URI
    from tests.unit.mcp.test_provider_output_compaction import _read_contract
    common = await _read_contract(create_direct_mcp(Mock()), COMMON_PREVIEW_RESOURCE_URI)
    for model in (DirectIfcGeometrySinglePreviewInput,):
        assert (
            model.model_fields["preview_profile_json"].description
            == GEOMETRY_PREVIEW_PROFILE_JSON_FIELD_DESCRIPTION
        )
    for model in (
        DirectIfcProductRealizationSolveProviderInput,
    ):
        assert (
            model.model_fields["preview_profile_json"].description
            == PRODUCT_REALIZATION_PREVIEW_PROFILE_JSON_FIELD_DESCRIPTION
        )
    assert (
        DirectIfcProductRealizationSolveProviderInput.model_fields["invocation_json"].description
        == PRODUCT_REALIZATION_INVOCATION_JSON_FIELD_DESCRIPTION
    )

    shared_required_text = (
        "presentation.panels",
        "camera_kind",
        "preset cameras, put view inside the camera object",
        "object discriminated by overlay_kind",
        "interface_overlays[].overlay_id",
        GEOMETRY_PREVIEW_PROFILE_MINIMAL_JSON,
        GEOMETRY_PREVIEW_PROFILE_TWO_PANEL_CLEAN_JSON,
        GEOMETRY_PREVIEW_PROFILE_INTERFACES_ALL_JSON,
        GEOMETRY_PREVIEW_PROFILE_TWO_PANEL_INTERFACES_TEMPLATE,
        GEOMETRY_PREVIEW_PROFILE_CONNECTION_AUTO_JSON,
        GEOMETRY_PREVIEW_PROFILE_CONNECTION_PIPES_JSON,
        "mode=none (default), auto or pipes",
        "connection_context_diagnostics",
        "three times the condition outer diameter",
        "without snapping or trimming",
    )
    for description in (
        IFC_GEOMETRY_PREVIEW_DESCRIPTION,
        IFC_PRODUCT_REALIZATION_PREVIEW_DESCRIPTION,
    ):
        assert COMMON_PREVIEW_RESOURCE_URI in description
        assert "render_metadata" not in description
    for required_text in shared_required_text:
        assert required_text in common["preview_profile"]

    for required_text in (
        "external_actual_connection_status remains NOT_CHECKED",
        "independent position, direction, roll and profile facts",
    ):
        assert required_text in common["preview_results"]

    from CAD2BIM_mcp.direct.ifc.geometry.contracts import DirectIfcGeometryCasesPreviewInput
    from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import DirectIfcProductRealizationCasesInput
    for model in (DirectIfcGeometrySinglePreviewInput, DirectIfcGeometryCasesPreviewInput,
                  DirectIfcProductRealizationSolveProviderInput, DirectIfcProductRealizationCasesInput):
        assert "#reference_geometries" in model.model_fields["reference_geometries"].description
        assert "expectation" in model.model_fields["retain"].description
    for description in (IFC_GEOMETRY_PREVIEW_DESCRIPTION, IFC_PRODUCT_REALIZATION_PREVIEW_DESCRIPTION):
        assert COMMON_PREVIEW_RESOURCE_URI in description
    assert "interface-frame checks do not establish material" in common["geometry_inspection"]
    assert "unverified required property" in common["geometry_inspection"]
    assert "increase vertical_extent_m or omit that optional field" in common["preview_profile"]
    assert "observed_interface" in common["preview_results"]
    assert "32 MiB" in common["reference_geometries"]


def test_geometry_saved_single_case_reuse_and_update_examples_parse_current_contracts():
    from CAD2BIM_mcp.direct.ifc.authoring_publish import DirectAuthoringPublishInput
    from CAD2BIM_mcp.direct.ifc.geometry.authoring_contracts import validate_geometry_resource_write_input
    from CAD2BIM_mcp.direct.ifc.preview_cases import parse_cases
    from CAD2BIM_mcp.tool_docs.ifc_geometry import GEOMETRY_RETAIN_REUSE_EXAMPLES
    from CAD2BIM_mcp.tool_docs.ifc_geometry import GEOMETRY_DRAFT_UPDATE_EXAMPLE, GEOMETRY_MODELING_FUNCTION_SOURCE_EXAMPLE

    examples = json.loads(json.dumps(GEOMETRY_RETAIN_REUSE_EXAMPLES).replace("<draft_ref>", "cur.draft").replace("<inspection_ref>", "cur.inspection"))
    single = validate_geometry_preview_input(examples["single"])
    batch = validate_geometry_preview_input(examples["cases"])
    case = parse_cases(batch.cases_json, case_kind="geometry").items[0]
    assert single.source == batch.source
    assert json.loads(single.sample_parameters_json) == case.parameters
    assert case.expectation.expected_status == "BUILT" and case.reuse.reuse_kind == "inspection"
    from pydantic import TypeAdapter
    from CAD2BIM_backend.domain.preview_cases import PreviewExecutionReuse
    TypeAdapter(PreviewExecutionReuse).validate_python(examples["alternative_reuse"])
    DirectAuthoringPublishInput.model_validate(examples["publish"])
    update = validate_geometry_resource_write_input({**GEOMETRY_DRAFT_UPDATE_EXAMPLE, "draft_ref": "cur.draft"})
    edit = update.source_changes[0].replacements[0]
    assert GEOMETRY_MODELING_FUNCTION_SOURCE_EXAMPLE.count(edit.old_text) == 1
    parameter_edit = update.to_domain_declaration()["parameter_schema"]
    assert parameter_edit["action"] == "edit"
    assert set(parameter_edit["set"]) == {"length_m"}
    assert parameter_edit["set"]["length_m"]["schema"]["maximum"] == 500.0


def test_documented_self_geometry_invocation_parses_without_host_facts():
    request = validate_product_realization_preview_provider_input({"mode": "single",
        "direct_session_alias": "direct_001", "attachment_kind": "self_geometry",
        "source": {"source_kind": "published", "family_version_alias": "cur.exact_family_version"},
        "invocation_json": PRODUCT_REALIZATION_SELF_GEOMETRY_INVOCATION_JSON,
    })
    assert request.product_origin == (2000, 3000, 4000)
    assert request.length_unit.name == "METRE"
    assert request.length_unit.prefix == "MILLI"
    assert request.external_constraints == ()
    assert "host_interfaces" not in request.model_dump()


@pytest.mark.parametrize("attachment_kind", ["standalone", "explicit_connection_interfaces", "explicit_host_interfaces", "current_ifc"])
def test_retired_family_invocation_modes_are_not_guide_entry_points(attachment_kind):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        validate_product_realization_preview_input({
            "direct_session_alias": "direct_001", "mode": "single", "attachment_kind": attachment_kind,
            "source": {"source_kind": "published", "family_version_alias": "cur.exact_family_version"},
            "invocation_json": PRODUCT_REALIZATION_PUBLIC_PARAMETERS_INVOCATION_JSON,
        })
