from __future__ import annotations


import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from importlib.metadata import version
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator
from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.tools import _convert_call_tool_result
from mcp.types import CallToolResult

from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    GeometryResourceScopeKind,
)
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_inspection import (
    GeometryInspectionDiagnostic, InspectionWholeSelection, InspectionResultCoordinates,
)
from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionPreviewOutcome
from CAD2BIM_backend.domain.geometry_runtime import (
    GeometryRenderCoordinateSpace,
    GeometryRenderInterfaceMarkerKind,
    GeometryRenderInterfaceOverlay,
    GeometryRenderedPreviewMetadata,
    GeometryResolvedOrthographicCamera,
    GeometryResolvedPreviewPanel,
    GeometryRuntimeResourceLimitError,
    GeometryPreview, GeometrySectionMeasurement, GeometrySectionResolvedPlane,
    GeometrySectionImage, GeometrySectionResult,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.preview_cases import project_case_batch
from CAD2BIM_mcp.direct.geometry_preview_contracts import project_preview_presentation
from CAD2BIM_mcp.direct.ifc.geometry.application import (
    IfcGeometryApplicationMixin,
    IfcGeometryPreviewAuthorization,
    _representative_geometry_sha256,
)
from CAD2BIM_mcp.direct.ifc.geometry.contracts import (
    DirectIfcGeometrySinglePreviewInput,
    DirectIfcGeometryInspectionPreviewInput,
    DirectIfcGeometrySinglePreviewInput,
    DirectIfcGeometryQueryFreshInput,
    DirectIfcGeometryResourceListFirstInput,
    IfcGeometryQueryPythonResult,
    IfcGeometryResourceVersionQueryResultTarget,
    IfcGeometryResourceVersionQueryTarget,
    IfcGeometryResourceInfoResult,
    IfcGeometryResourceListResult,
    IfcGeometryResourceRootInfoResult,
    IfcGeometryResourceSourceReadResult,
)
from CAD2BIM_mcp.direct.ifc.geometry.tools import IFC_GEOMETRY_TOOL_REGISTRARS
from CAD2BIM_mcp.tool_docs import ifc_geometry as _geometry_tool_docs


_TOOL_NAMES = {
    "ifc_geometry_resource_list",
    "ifc_geometry_resource_info",
    "ifc_geometry_resource_source_read",
    "ifc_geometry_preview",
    "ifc_geometry_query_python",
}
_FORBIDDEN_SCHEMA_KEYS = {"allOf", "if", "then", "else", "not"}
_INTERNAL_NAMES = {
    "resource_id",
    "resource_version_id",
    "artifact_id",
    "artifact_version_id",
    "storage_path",
    "task_id",
    "run_id",
    "tenant_id",
    "model_id",
    "revision_id",
}
_SHA = "a" * 64
_RESOURCE_REF = "cur.resource_ref_001"
_VERSION_REF = "cur.version_ref_001"
_ATTEMPT_REF = "cur.attempt_ref_001"
_PREVIEW_BYTES = b"\x89PNG\r\n\x1a\nvisual-preview-pixels"
_PREVIEW_SHA = hashlib.sha256(_PREVIEW_BYTES).hexdigest()


def test_geometry_authoring_rejects_unsupported_parameter_contract_before_application() -> None:
    from CAD2BIM_mcp.direct.ifc.geometry.authoring_contracts import validate_geometry_resource_write_input
    from CAD2BIM_mcp.tool_docs.ifc_geometry import GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE
    value = json.loads(GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE)
    value["source"]["parameter_schema_json"] = json.dumps({"action": "set", "value": {
        "type": "object", "additionalProperties": False,
        "properties": {"value": {"type": "array", "items": {"type": "number"}}}, "required": ["value"]}})
    with pytest.raises(ValidationError, match="arrays require"):
        validate_geometry_resource_write_input(value).to_domain_declaration()


def _batch_outcome(*, retained=False):
    from CAD2BIM_backend.domain.preview_cases import PreviewCaseBatchOutcome, PreviewCaseReport, PreviewCaseResult, GeometryPreviewCase, PreviewCaseDiagnostic
    from CAD2BIM_backend.domain.geometry_query import GeometryQueryArtifactRef
    from tests.unit.backend.test_preview_cases import _source
    return PreviewCaseBatchOutcome(report=PreviewCaseReport(source=_source("geometry_sample"), cases=(
        PreviewCaseResult(case=GeometryPreviewCase(case_key="observation"), execution_status="completed",
            expectation_check="not_checked", geometry_status="BUILT", render_status="failed" if retained else "not_requested",
            inspection_ref="cur.inspection_exact" if retained else None,
            inspection_artifact=GeometryQueryArtifactRef(artifact_id="artifact", version_id="version", sha256="a" * 64) if retained else None,
            diagnostics=(PreviewCaseDiagnostic(code="PREVIEW_RENDER_FAILED", phase="render", message="Rendering failed."),) if retained else ()),)),
        operation_alias="op_019" if retained else None)


def _render_metadata() -> GeometryRenderedPreviewMetadata:
    return GeometryRenderedPreviewMetadata(
        scene_sha256="1" * 64,
        coordinate_space=GeometryRenderCoordinateSpace.GEOMETRY_COORDINATES,
        resolved_panels=(
            GeometryResolvedPreviewPanel(
                panel_key="front",
                x_px=0,
                y_px=0,
                width_px=640,
                height_px=480,
                camera=GeometryResolvedOrthographicCamera(
                    target_m=(0.0, 0.0, 0.0),
                    direction=(0.0, 1.0, 0.0),
                    up=(0.0, 0.0, 1.0),
                    right=(1.0, 0.0, 0.0),
                    horizontal_extent_m=2.0,
                    vertical_extent_m=1.5,
                ),
            ),
        ),
    )


class _GeometryFacadeProbe(IfcGeometryApplicationMixin):
    def __init__(self, scope: object) -> None:
        self.scope = scope

    async def _geometry_read(self, _alias, _action, operation, **_options):
        return await operation(self.scope)


@pytest.mark.parametrize(
    "preview_input",
    (
        DirectIfcGeometrySinglePreviewInput(
            direct_session_alias="direct_001",
            mode="single",
            source={"source_kind": "published", "resource_version_alias": _VERSION_REF},
        ),
        DirectIfcGeometrySinglePreviewInput(
            direct_session_alias="direct_001",
            mode="single",
            source={"source_kind": "published", "resource_version_alias": _VERSION_REF},
            sample_parameters_json="{}",
        ),
    ),
    ids=("mapped-model", "modeling-function"),
)
async def test_geometry_preview_branches_cover_admission_execution_and_response(
    preview_input,
) -> None:
    application = IfcGeometryApplicationMixin()
    run_read = AsyncMock(return_value="preview-result")
    application._run_runtime_read = run_read

    assert await application.preview_ifc_geometry(preview_input) == "preview-result"
    assert run_read.await_args.args[:2] == (
        "direct_001",
        "ifc_geometry_preview",
    )
    assert run_read.await_args.kwargs == {
        "timeout_seconds": 780.0,
        "timeout_code": "GEOMETRY_PREVIEW_TIMEOUT",
        "timeout_message": "The Direct geometry-preview total deadline expired",
    }


@pytest.mark.asyncio
async def test_invalid_focus_request_is_projected_as_input_error() -> None:
    from CAD2BIM_backend.domain.geometry_runtime import GeometryPreviewRequestError

    scope = SimpleNamespace(identity=object())
    application = IfcGeometryApplicationMixin()

    async def run_read(_alias, _action, operation, **_options):
        return await operation(scope)

    application._run_runtime_read = run_read
    application._geometry_preview_service = SimpleNamespace(
        prepare_published_cases=AsyncMock(return_value={}),
        preview_loaded=AsyncMock(side_effect=GeometryPreviewRequestError("increase vertical_extent_m")),
    )
    request = DirectIfcGeometrySinglePreviewInput(
        direct_session_alias="direct_001",
        mode="single",
        source={"source_kind": "published", "resource_version_alias": _VERSION_REF},
    )
    with pytest.raises(DirectApplicationError) as captured:
        await application.preview_ifc_geometry(request)
    assert captured.value.code == "GEOMETRY_PREVIEW_REQUEST_INVALID"
    assert captured.value.phase == "input_validation"
    assert captured.value.retryable is False
    assert "increase vertical_extent_m" in str(captured.value)


@pytest.mark.asyncio
async def test_started_preview_resource_limit_preserves_code_and_is_not_retryable() -> None:
    scope = SimpleNamespace(identity=object())
    application = IfcGeometryApplicationMixin()

    async def run_read(_alias, _action, operation, **_options):
        return await operation(scope)

    application._run_runtime_read = run_read
    application._geometry_preview_service = SimpleNamespace(
        prepare_published_cases=AsyncMock(return_value={}),
        preview_loaded=AsyncMock(
            side_effect=GeometryRuntimeResourceLimitError("private native limit detail")
        )
    )
    request = DirectIfcGeometrySinglePreviewInput(
        direct_session_alias="direct_001",
        mode="single",
        source={"source_kind": "published", "resource_version_alias": _VERSION_REF},
    )

    with pytest.raises(DirectApplicationError) as captured:
        await application.preview_ifc_geometry(request)

    assert captured.value.code == "GEOMETRY_RUNTIME_UNAVAILABLE"
    assert captured.value.phase == "execution"
    assert captured.value.retryable is False
    assert "private native limit detail" not in str(captured.value)


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def _assert_safe_schema(schema: Mapping[str, object]) -> None:
    Draft202012Validator.check_schema(schema)
    for node in _walk(schema):
        if isinstance(node, Mapping):
            assert node != {}
            assert not (_FORBIDDEN_SCHEMA_KEYS & set(node))
            assert node.get("type") != "unknown"
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False
    serialized = json.dumps(schema, sort_keys=True).lower()
    for name in _INTERNAL_NAMES:
        assert f'"{name}"' not in serialized


def _results() -> SimpleNamespace:
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    listing = IfcGeometryResourceListResult(resources=(), complete=True, next_cursor=None)
    info = IfcGeometryResourceInfoResult(
        info=IfcGeometryResourceRootInfoResult(
            target_kind="resource_root",
            resource_alias=_RESOURCE_REF,
            resource_kind="mapped_model",
            owner_scope="system",
            display_name="Neutral elbow",
            summary="Neutral reusable mapped geometry.",
            versions=(),
        )
    )
    source = IfcGeometryResourceSourceReadResult(
        resource_version_alias=_VERSION_REF,
        source_path="main.py",
        view_mode="full",
        source_sha256=_SHA,
        total_lines=1,
        text="shape = make_shape()\n",
        returned_start_line=1,
        returned_line_count=1,
        match_count=0,
        truncated=False,
    )
    preview = project_case_batch(_batch_outcome())
    query = IfcGeometryQueryPythonResult(
        attempt_alias=_ATTEMPT_REF,
        parent_attempt_alias=None,
        target=IfcGeometryResourceVersionQueryResultTarget(
            target_kind="geometry_resource_version",
            resource_version_alias=_VERSION_REF,
        ),
        source_sha256=_SHA,
        status="succeeded",
        summary="One bounded result.",
        result_json='[{"solid_count":1}]',
        next_cursor=None,
    )
    return SimpleNamespace(
        now=now,
        listing=listing,
        info=info,
        source=source,
        preview=preview,
        query=query,
    )


def _application() -> SimpleNamespace:
    values = _results()
    return SimpleNamespace(
        list_ifc_geometry_resources=AsyncMock(return_value=values.listing),
        get_ifc_geometry_resource_info=AsyncMock(return_value=values.info),
        read_ifc_geometry_resource_source=AsyncMock(return_value=values.source),
        preview_ifc_geometry=AsyncMock(
            return_value=IfcGeometryPreviewAuthorization(
                preview=values.preview,
                content=_PREVIEW_BYTES,
            )
        ),
        query_ifc_geometry_python=AsyncMock(return_value=values.query),
    )


def _server(application: object) -> FastMCP:
    server = FastMCP(
        name="IFC Geometry Provider Test",
        strict_input_validation=True,
        mask_error_details=True,
    )
    for registrar in IFC_GEOMETRY_TOOL_REGISTRARS:
        registrar(server, application)
    return server


def _preview_json(response):
    assert response.structured_content is None
    assert response.content[0].type == "text"
    return json.loads(response.content[0].text)


async def test_i0b302_geometry_schemas_are_closed_discriminated_and_native_visible() -> None:
    server = _server(_application())
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert set(tools) == _TOOL_NAMES

    list_branches = tools["ifc_geometry_resource_list"].parameters["anyOf"]
    assert tuple(list_branches[0]["properties"]) == (
        "direct_session_alias",
        "resource_kinds",
        "page_size",
    )
    assert tuple(list_branches[1]["properties"]) == ("direct_session_alias", "cursor")

    target_branches = tools["ifc_geometry_resource_info"].parameters["properties"]["target"][
        "anyOf"
    ]
    assert [branch["properties"]["target_kind"]["const"] for branch in target_branches] == [
        "resource_root",
        "resource_version",
        "draft",
    ]
    assert all("target_kind" in branch["required"] for branch in target_branches)

    source_branches = tools["ifc_geometry_resource_source_read"].parameters["anyOf"]
    assert [branch["properties"]["view_mode"]["const"] for branch in source_branches] == [
        "full",
        "lines",
        "search",
    ]
    assert all("view_mode" in branch["required"] for branch in source_branches)

    query_branches = tools["ifc_geometry_query_python"].parameters["anyOf"]
    assert [branch["properties"]["mode"]["const"] for branch in query_branches] == [
        "fresh",
        "repair",
        "cursor",
    ]
    assert all("mode" in branch["required"] for branch in query_branches)
    assert "target" in query_branches[0]["required"]
    query_target = query_branches[0]["properties"]["target"]
    query_targets = query_target["anyOf"]
    assert [item["properties"]["target_kind"]["const"] for item in query_targets] == [
        "geometry_resource_version", "geometry_inspection", "family_standard_model",
    ]
    assert tuple(query_targets[0]["properties"]) == (
        "target_kind",
        "resource_version_alias",
    )
    assert "target_kind" in query_targets[0]["required"]
    assert set(query_targets[1]["required"]) == {"target_kind", "inspection_ref", "selection", "coordinate_space"}
    assert set(query_targets[2]["required"]) == {"target_kind", "family_version_alias"}
    assert "parent_attempt_alias" in query_branches[1]["required"]
    assert "target" not in query_branches[1]["properties"]
    assert "resource_version_alias" not in query_branches[1]["properties"]
    assert tuple(query_branches[2]["properties"]) == (
        "direct_session_alias",
        "mode",
        "cursor",
    )

    query_result_target = tools["ifc_geometry_query_python"].output_schema["properties"]["target"]
    query_result_targets = query_result_target["anyOf"]
    assert query_result_targets[0]["properties"]["target_kind"]["const"] == "geometry_resource_version"
    assert "resource_version_alias" in query_result_targets[0]["required"]
    assert query_result_targets[1]["properties"]["target_kind"]["const"] == "geometry_inspection"

    preview = tools["ifc_geometry_preview"]
    assert preview.output_schema is None
    async with Client(server) as client:
        schema_contents = await client.read_resource("cad2bim://contracts/ifc_geometry_preview/result")
    result_guide = json.loads(schema_contents[0].text)
    assert "$defs" not in result_guide
    assert "section" in result_guide["saved_observation"]
    from CAD2BIM_mcp.direct.ifc.geometry.contracts import IfcGeometryPreviewOutcomeResult
    from CAD2BIM_mcp.direct.provider_schema import provider_output_schema
    from CAD2BIM_mcp.direct.references import reference_schema_projection
    preview_schema = reference_schema_projection(provider_output_schema(
        IfcGeometryPreviewOutcomeResult, compact=True, preserve_references=True,
    ))[0]
    from tests.unit.mcp.test_provider_output_compaction import _expand_shared_input
    preview_branches = _expand_shared_input(preview.parameters)["oneOf"]
    assert [branch["properties"]["mode"]["const"] for branch in preview_branches] == ["single", "cases", "inspection", "family_standard_model"]
    assert set(preview_branches[0]["properties"]) == {"direct_session_alias", "mode", "source", "sample_parameters_json",
        "preview_profile_json", "datum_transform", "reference_geometries", "retain"}
    assert set(preview_branches[1]["properties"]) == {"direct_session_alias", "mode", "source", "cases_json",
        "render_json", "datum_transform", "reference_geometries", "retain"}
    assert set(preview_branches[2]["required"]) == {"direct_session_alias", "mode", "inspection_ref", "selection", "coordinate_space", "observation"}
    assert set(preview_branches[3]["required"]) == {"direct_session_alias", "mode", "family_version_alias", "observation"}
    coordinate_description = preview_branches[2]["properties"]["coordinate_space"]["description"]
    assert "context.source.result_coordinate_space" in coordinate_description
    assert "without datum uses geometry_local" in coordinate_description
    assert "sample with datum" in coordinate_description and "project_world" in coordinate_description
    assert "receipt scale already applied" in coordinate_description
    _assert_safe_schema(preview_schema)
    preview_schema = preview_schema["$defs"]["PreviewCaseBatchResult"]
    assert "source_ref" in preview_schema["properties"] and "snapshot_id" not in preview_schema["properties"]
    assert "cases" in preview_schema["properties"] and "rendered_case_keys" in preview_schema["properties"]
    assert "geometry_preview_capability" not in preview_schema["properties"]
    assert "resource-local coordinates with identity placement" in preview_branches[0]["properties"]["datum_transform"]["description"]
    _assert_safe_schema(preview_schema)
    assert "geometry and independently evaluated interfaces" in preview.description
    assert "same normalized parameters" in preview.description
    assert "temporary_interfaces" in preview.description
    assert "inline ImageContent" in preview.description

    info_output_branches = tools["ifc_geometry_resource_info"].output_schema["properties"]["info"][
        "anyOf"
    ]
    version_output = next(
        branch
        for branch in info_output_branches
        if branch["properties"]["target_kind"]["const"] == "resource_version"
    )
    assert "content_sha256" not in version_output["properties"]
    assert tuple(
        version_output["properties"]["geometry_inputs"]["items"]["properties"]
    ) == ("input_key", "resource_version_alias")
    source_file = version_output["properties"]["source_files"]["items"]
    assert tuple(source_file["properties"]) == (
        "source_path",
        "size_bytes",
        "sha256",
        "text_available",
    )
    assert "text_available" in source_file["required"]

    source_description = tools["ifc_geometry_resource_source_read"].description
    assert "text_available=false" in source_description
    assert "GEOMETRY_SOURCE_NOT_TEXT" in source_description
    assert "ifc_geometry_query_python" in source_description

    query_tool = tools["ifc_geometry_query_python"]
    query_description = query_tool.description
    assert _geometry_tool_docs.GEOMETRY_QUERY_PROGRAM_RESOURCE_URI in query_description
    assert "target Schema" in query_description
    assert "native cadquery.Shape" in query_description
    assert "All coordinates are METRE" in query_description
    assert "Only a top-level list is paged" in query_description
    assert "resource_diagnostic" in query_description
    assert "Executable exploratory parameter-sampling example" not in query_description
    assert "GEOMETRY_QUERY_RESOURCE_LIMIT" in query_description
    assert "terminal and non-retryable" in query_description
    assert "modeling_function and one-time Preview images are not query targets" in query_description
    assert "Neither branch accepts a resource alias" in query_description
    assert "shape = ctx.private_geometry()" in query_description

    fresh_code_description = query_branches[0]["properties"]["code"]["description"]
    assert "defining query(ctx)" in fresh_code_description
    assert "selected exact private target" in fresh_code_description
    assert "in METRE coordinates" in fresh_code_description
    assert "JSON-compatible" in fresh_code_description
    assert _geometry_tool_docs.GEOMETRY_QUERY_PROGRAM_RESOURCE_URI in fresh_code_description
    assert "cannot publish or modify Geometry or IFC" in fresh_code_description

    for tool in tools.values():
        assert tool.annotations.readOnlyHint is (tool.name != "ifc_geometry_preview")
        assert tool.annotations.destructiveHint is False
        assert "Multiple independent READ calls may be submitted together" in tool.description
        assert "Do not submit a WRITE in the same batch" in tool.description
        _assert_safe_schema(tool.parameters)
        for node in _walk(tool.parameters):
            if isinstance(node, Mapping) and "title" in node:
                # The shared generator preserves explicit enum labels, not inferred model/field titles.
                assert "enum" in node
                assert node["title"] in {"GeometryPreviewView", "GeometryPreviewInterfaceFocusOrientation"}
        if tool.name != "ifc_geometry_preview":
            assert tool.output_schema is not None
            _assert_safe_schema(tool.output_schema)

    descriptions = " ".join(tool.description for tool in tools.values())
    assert "trusted" in descriptions
    assert "separate supervised child processes" in _geometry_tool_docs.GEOMETRY_AUTHORING_EXECUTION_AND_DIAGNOSTIC_GUIDANCE
    assert "exact" in descriptions
    assert "no geometry publication path" in descriptions
    assert "source input" in descriptions
    topology_branches = version_output["properties"]["topology"]["anyOf"]
    assert any(branch.get("type") == "null" for branch in topology_branches)
    topology_schema = next(branch for branch in topology_branches if branch.get("type") == "object")
    triangle_schema = topology_schema["properties"]["triangle_count"]
    assert [branch["type"] for branch in triangle_schema["anyOf"]] == ["integer", "null"]
    assert "fresh" in descriptions and "repair" in descriptions and "cursor" in descriptions

    async with Client(server) as client:
        native = {tool.name: tool for tool in await client.list_tools()}
        program_contents = await client.read_resource(
            _geometry_tool_docs.GEOMETRY_QUERY_PROGRAM_RESOURCE_URI
        )
    program = json.loads(program_contents[0].text)
    assert set(program) == {"usage", "target_schemas", "return_contract", "examples"}
    usage = program["usage"]
    for method in (
        "summary()",
        "private_geometry()",
        "vertices()",
        "triangles()",
        "face_types()",
        "edge_types()",
    ):
        assert method in usage
    for phrase in (
        "cadquery.Shape",
        ".wrapped",
        "math, cadquery, and OCP",
        "right-handed local",
        "reflection including dir",
        "Module scope permits imports, function definitions, and literal constants only",
        "put calculations inside query(ctx) or helpers",
        "Top-level classes or executable statements",
        "65,536 UTF-8 bytes",
        "180 seconds wall",
        "180 seconds CPU",
        "3 GiB RSS",
        "128 MiB",
        "8 threads",
        "64 open file descriptors",
        "admission budget",
        "10 seconds for response projection",
        "resource_diagnostic",
        "radius() requires an analytic circle",
        "Native curve parameters generally differ from arc length",
        "Finite samples do not certify global distance",
    ):
        assert phrase in usage
    target_schemas = program["target_schemas"]
    assert tuple(target_schemas) == ("geometry_resource_version", "geometry_inspection", "family_standard_model")
    for target_kind, schema in target_schemas.items():
        assert schema["properties"]["target_kind"]["const"] == target_kind
        assert "target_kind" in schema["required"]
        assert schema["additionalProperties"] is False
    assert tuple(target_schemas["geometry_resource_version"]["properties"]) == (
        "target_kind",
        "resource_version_alias",
    )
    value = {"target_kind": "geometry_resource_version", "resource_version_alias": "gv_" + "A" * 22}
    resource_validator = Draft202012Validator(target_schemas["geometry_resource_version"])
    provider_validator = Draft202012Validator(query_target)
    assert list(resource_validator.iter_errors(value)) == []
    assert list(provider_validator.iter_errors(value)) == []
    assert target_schemas["geometry_resource_version"]["properties"]["resource_version_alias"]["pattern"] == query_targets[0]["properties"]["resource_version_alias"]["pattern"]
    assert list(resource_validator.iter_errors({**value, "target_kind": "wrong_target_kind"}))
    assert list(resource_validator.iter_errors({**value, "resource_version_alias": "gp_" + "X" * 22}))
    assert "native cadquery.Shape" in program["return_contract"]
    assert "query(ctx) must return JSON-compatible data" in program["return_contract"]
    assert "IfcGeometryQueryPythonResult" in program["return_contract"]
    assert program["examples"] == {
        "bounding_box": json.loads(_geometry_tool_docs.GEOMETRY_QUERY_FRESH_EXAMPLE),
        "display_diagnostics": _geometry_tool_docs.GEOMETRY_QUERY_DISPLAY_DIAGNOSTICS_EXAMPLE,
        "plane_section": json.loads(_geometry_tool_docs.GEOMETRY_QUERY_SECTION_FRESH_EXAMPLE),
    }
    assert (
        program["examples"]["bounding_box"]["code"]
        == _geometry_tool_docs.GEOMETRY_QUERY_SOURCE_EXAMPLE
    )
    assert (
        program["examples"]["plane_section"]["code"]
        == _geometry_tool_docs.GEOMETRY_QUERY_SECTION_SOURCE_EXAMPLE
    )
    for name, tool in tools.items():
        assert native[name].inputSchema == tool.parameters
        assert native[name].outputSchema == tool.output_schema
        assert native[name].description == tool.description


async def test_i0b302_geometry_tools_dispatch_complete_provider_branches() -> None:
    application = _application()
    server = _server(application)

    async with Client(server) as client:
        listing = await client.call_tool(
            "ifc_geometry_resource_list",
            {"direct_session_alias": "direct_001", "resource_kinds": ["mapped_model"]},
        )
        info = await client.call_tool(
            "ifc_geometry_resource_info",
            {
                "direct_session_alias": "direct_001",
                "target": {"target_kind": "resource_root", "resource_alias": _RESOURCE_REF},
            },
        )
        source = await client.call_tool(
            "ifc_geometry_resource_source_read",
            {
                "direct_session_alias": "direct_001",
                "resource_version_alias": _VERSION_REF,
                "source_path": "main.py",
                "view_mode": "lines",
                "start_line": 1,
                "line_count": 20,
            },
        )
        query = await client.call_tool(
            "ifc_geometry_query_python",
            {
                "direct_session_alias": "direct_001",
                "mode": "fresh",
                "target": {
                    "target_kind": "geometry_resource_version",
                    "resource_version_alias": _VERSION_REF,
                },
                "code": "def query(ctx):\n    return ctx.summary()\n",
            },
        )

    assert listing.structured_content == _results().listing.model_dump(mode="json")
    assert info.structured_content == _results().info.model_dump(mode="json")
    assert source.structured_content == _results().source.model_dump(mode="json")
    assert query.structured_content == _results().query.model_dump(mode="json")

    list_request = application.list_ifc_geometry_resources.await_args.args[0]
    assert isinstance(list_request, DirectIfcGeometryResourceListFirstInput)
    assert list_request.resource_kinds == ("mapped_model",)
    resource_request = application.query_ifc_geometry_python.await_args_list[0].args[0]
    assert isinstance(resource_request, DirectIfcGeometryQueryFreshInput)
    assert isinstance(resource_request.target, IfcGeometryResourceVersionQueryTarget)
    assert resource_request.target.resource_version_alias == _VERSION_REF
    request_identity = application.query_ifc_geometry_python.await_args_list[0].kwargs[
        "request_identity"
    ]
    assert isinstance(request_identity, str) and request_identity


async def test_geometry_preview_returns_inline_image_without_resource_link() -> None:
    application = _application()
    server = _server(application)

    async with Client(server) as client:
        response = await client.call_tool(
            "ifc_geometry_preview",
            {
                "direct_session_alias": "direct_001",
                "mode": "single",
                "source": {"source_kind": "published", "resource_version_alias": _VERSION_REF},
            },
        )

    assert response.structured_content is None
    assert json.loads(response.content[0].text) == _results().preview.model_dump(mode="json", exclude_none=True)
    assert [item.type for item in response.content] == ["text", "image"]
    images = [item for item in response.content if item.type == "image"]
    assert len(images) == 1
    assert images[0].mimeType == "image/png"
    assert base64.b64decode(images[0].data, validate=True) == _PREVIEW_BYTES
    assert "content_sha256" not in _preview_json(response)
    assert "geometry_preview_capability" not in _preview_json(response)


async def test_saved_geometry_preview_projects_public_panels_and_display_warnings() -> None:
    from CAD2BIM_backend.domain.geometry_runtime import GeometryDisplayQualityWarning
    from CAD2BIM_mcp.direct.ifc.geometry.application import _project_inspection_preview
    warning = GeometryDisplayQualityWarning(geometry_sha256=_SHA, face_index=3, native_area_m2=1,
        message="Selected fragment 'shell' in result: Face 3 (PLANE), selected=retry; initial area difference=1, retry=.2.",
        initial_mesh_area_m2=2, retry_mesh_area_m2=1.2, initial_relative_difference=1,
        retry_relative_difference=0.2, selected_attempt="retry")
    image = GeometryPreview(width_px=640, height_px=480, payload=_PREVIEW_BYTES, sha256=_PREVIEW_SHA,
        resolved_panels=_render_metadata().resolved_panels, display_quality_warnings=(warning,),
        connection_context={"mode": "pipes", "items": [{"start_m": [0, 0, 0],
            "end_m": [0, 0, 0.1], "outer_diameter_m": 0.05}]},
        connection_context_diagnostics=("No automatic installation targets are saved for this Geometry.",))
    projected = _project_inspection_preview(_inspection_outcome(preview=image))
    application = _application()
    application.preview_ifc_geometry.return_value = projected
    async with Client(_server(application)) as client:
        response = await client.call_tool("ifc_geometry_preview", {
            "direct_session_alias": "direct_001", "mode": "inspection", "inspection_ref": "cur.inspection_exact",
            "selection": {"selection_kind": "whole"}, "coordinate_space": {"coordinate_space": "result"},
            "observation": {"observation_kind": "model"},
        })
    image_result = _preview_json(response)["image"]
    assert image_result["panels"][0]["panel_key"] == "front"
    assert set(image_result["display_quality_warnings"][0]) == {"code", "message"}
    assert image_result["display_quality_warnings"][0]["message"] == warning.message
    assert "geometry_sha256" not in image_result["display_quality_warnings"][0]
    assert image_result["connection_context"]["items"][0]["outer_diameter_m"] == 0.05
    assert image_result["connection_context_diagnostics"] == [
        "No automatic installation targets are saved for this Geometry."]
    assert base64.b64decode(response.content[1].data) == _PREVIEW_BYTES


@pytest.mark.parametrize(("document", "reason"), [
    ('{"presentation":{"panels":[],"private_value":"do_not_echo"}', "must contain valid JSON"),
    ('{"private_value":"do_not_echo","private_value":0}', "contains a duplicate object key"),
    ('["do_not_echo"]', "must contain one JSON object"),
])
async def test_preview_invalid_json_reports_the_selected_branch_field(
    document: str, reason: str,
) -> None:
    application = _application()
    arguments = {
        "direct_session_alias": "direct_001",
        "mode": "single",
        "source": {"source_kind": "published", "resource_version_alias": _VERSION_REF},
        "sample_parameters_json": '{"offset_x_m":0.007}',
        "preview_profile_json": document,
    }
    async with Client(_server(application)) as client:
        result = await client.call_tool("ifc_geometry_preview", arguments, raise_on_error=False)
    message = " ".join(item.text for item in result.content if item.type == "text")
    assert result.is_error
    assert "INVALID_ARGUMENT" in message
    assert "do_not_echo" not in message and "input_value" not in message
    application.preview_ifc_geometry.assert_not_awaited()


async def test_i0b303_function_preview_also_returns_inline_exact_image() -> None:
    application = _application()
    server = _server(application)

    async with Client(server) as client:
        response = await client.call_tool(
            "ifc_geometry_preview",
            {
                "direct_session_alias": "direct_001",
                "mode": "single",
                "source": {"source_kind": "published", "resource_version_alias": _VERSION_REF},
                "sample_parameters_json": '{"diameter_m":0.2}',
            },
        )

    assert [item.type for item in response.content] == ["text", "image"]
    image = response.content[1]
    assert image.type == "image"
    assert base64.b64decode(image.data, validate=True) == _PREVIEW_BYTES
    request = application.preview_ifc_geometry.await_args.args[0]
    assert isinstance(request, DirectIfcGeometrySinglePreviewInput)
    assert request.sample_parameters_json == '{"diameter_m":0.2}'




@pytest.mark.parametrize(
    "preview_profile_json",
    [
        '{"view":"front","view":"back"}',
        '{"width_px":NaN}',
        '{"presentation":{"panels":[],"overlay":{"overlay_kind":"clean"}}}',
        '{"presentation":{"panels":[{"panel_key":"front","camera":'
        '{"camera_kind":"preset_fit","view":"front","extra":true}}],'
        '"overlay":{"overlay_kind":"clean"}}}',
        '{"presentation":{"panels":[{"panel_key":"front","camera":'
        '{"camera_kind":"preset_fit","view":"front"}}],'
        '"overlay":{"overlay_kind":"interfaces","temporary_interfaces":["\\ud800"]}}}',
    ],
)
async def test_p4_geometry_profile_rejects_ambiguous_or_invalid_json(
    preview_profile_json: str,
) -> None:
    application = _application()
    server = _server(application)

    async with Client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "ifc_geometry_preview",
                {
                    "direct_session_alias": "direct_001",
                    "mode": "single",
                    "source": {"source_kind": "published", "resource_version_alias": _VERSION_REF},
                    "preview_profile_json": preview_profile_json,
                },
            )

    application.preview_ifc_geometry.assert_not_awaited()


async def test_preview_failure_returns_no_partial_or_synthetic_image() -> None:
    application = _application()
    application.preview_ifc_geometry.side_effect = DirectApplicationError(
        "GEOMETRY_PREVIEW_UNAVAILABLE",
        "geometry runtime is unavailable",
        phase="execution",
    )
    server = _server(application)

    async with Client(server) as client:
        with pytest.raises(ToolError, match="GEOMETRY_PREVIEW_UNAVAILABLE") as caught:
            await client.call_tool(
                "ifc_geometry_preview",
                {
                    "direct_session_alias": "direct_001",
                    "mode": "single",
                    "source": {"source_kind": "published", "resource_version_alias": _VERSION_REF},
                },
            )

    assert "retryable=false" in str(caught.value)


async def test_locked_langchain_adapter_places_inline_png_in_tool_message_content() -> None:
    assert version("langchain-mcp-adapters") == "0.3.0"
    application = _application()
    server = _server(application)
    async with Client(server) as client:
        response = await client.call_tool(
            "ifc_geometry_preview",
            {
                "direct_session_alias": "direct_001",
                "mode": "single",
                "source": {"source_kind": "published", "resource_version_alias": _VERSION_REF},
            },
        )

    converted, artifact = _convert_call_tool_result(
        CallToolResult(
            content=response.content,
            structuredContent=response.structured_content,
        )
    )
    message = ToolMessage(content=converted, tool_call_id="preview-call")
    assert isinstance(message.content, list)
    image = message.content[1]
    assert image["type"] == "image"
    assert image["base64"] == base64.b64encode(_PREVIEW_BYTES).decode("ascii")
    assert image["mime_type"] == "image/png"
    assert "url" not in image
    assert artifact is None
    assert json.loads(message.content[0]["text"]) == _preview_json(response)


def test_geometry_datum_is_rigid_and_inspect_is_retired() -> None:
    from pydantic import ValidationError
    from CAD2BIM_mcp.direct.ifc.geometry.contracts import validate_geometry_preview_input

    base = {"direct_session_alias": "direct_001", "mode": "single", "source": {"source_kind": "published", "resource_version_alias": _VERSION_REF}}
    with pytest.raises(ValidationError, match="orthonormal"):
        validate_geometry_preview_input({**base, "datum_transform": {"rotation": [[2,0,0],[0,1,0],[0,0,1]]}})
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        validate_geometry_preview_input({"direct_session_alias": "direct_001", "mode": "inspect", "geometry_preview_capability": "cur.old", "datum_transform": {}})


@pytest.mark.parametrize("parameters", [{}, {"diameter_m": 0.2}])
async def test_direct_facade_preview_resolves_source_and_parses_one_sample(parameters) -> None:
    scope = SimpleNamespace(identity=object())
    application = _GeometryFacadeProbe(scope)
    service = SimpleNamespace(prepare_published_cases=AsyncMock(return_value={"source": "exact_snapshot"}),
        preview_loaded=AsyncMock(return_value=_batch_outcome()))
    application._geometry_preview_service = service
    result = await application.preview_ifc_geometry(DirectIfcGeometrySinglePreviewInput(
        direct_session_alias="direct_001", mode="single", source={"source_kind": "published", "resource_version_alias": _VERSION_REF},
        sample_parameters_json=json.dumps(parameters)))
    service.prepare_published_cases.assert_awaited_once_with(scope, _VERSION_REF)
    assert service.preview_loaded.await_args.kwargs["parameters"] == parameters
    assert service.preview_loaded.await_args.kwargs["source"] == "exact_snapshot"
    assert result.preview.cases[0].status == "BUILT"


def test_inspection_preview_rejects_source_override_and_inconsistent_local_coordinates() -> None:
    from pydantic import ValidationError

    base = {
        "direct_session_alias": "direct_001", "mode": "inspection",
        "inspection_ref": "cur.inspection_exact", "selection": {"selection_kind": "whole"},
        "coordinate_space": {"coordinate_space": "result"},
        "observation": {"observation_kind": "section", "plane": {"origin_m": [0, 0, 0], "normal": [0, 0, 1]}},
    }
    request = DirectIfcGeometryInspectionPreviewInput.model_validate(base)
    assert request.observation.offsets_m == (0.0,)
    for change in (
        {"resource_version_alias": _VERSION_REF},
        {"sample_parameters_json": "{}"},
        {"datum_transform": {}},
        {"coordinate_space": {"coordinate_space": "fragment_local", "fragment_key": "body"}},
        {"selection": {"selection_kind": "fragment", "fragment_key": "body"},
         "coordinate_space": {"coordinate_space": "fragment_local", "fragment_key": "other"}},
        {"observation": {"observation_kind": "section", "plane": {"origin_m": [0, 0, 0], "normal": [0, 0, 0]}}},
        {"observation": {"observation_kind": "section", "plane": {"origin_m": [0, 0, 0], "normal": [0, 0, 1]}, "offsets_m": [0, 0]}},
    ):
        with pytest.raises(ValidationError):
            DirectIfcGeometryInspectionPreviewInput.model_validate({**base, **change})


def _inspection_outcome(*, preview=None, section=None, retained=False):
    return GeometryInspectionPreviewOutcome(
        inspection_ref="cur.inspection_exact", source_kind="geometry_sample",
        source_version_ref=_VERSION_REF, result_coordinate_space="geometry_local",
        selection=InspectionWholeSelection(), coordinates=InspectionResultCoordinates(),
        preview=preview, section=section, operation_alias="op_019" if retained else None,
        diagnostics=(() if preview is not None or section is not None else (
            GeometryInspectionDiagnostic(code="PREVIEW_RENDER_FAILED", phase="render",
                message="Rendering failed after the actual result was retained.",
                action="Observe the same inspection_ref with another display."),
        )),
    )


async def test_retained_function_keeps_stable_identity_and_text_only_render_failure() -> None:
    application = _GeometryFacadeProbe(SimpleNamespace(identity=object()))
    service = SimpleNamespace(prepare_published_cases=AsyncMock(return_value={}),
        preview_loaded=AsyncMock(return_value=_batch_outcome(retained=True)))
    application._geometry_preview_service = service
    request = DirectIfcGeometrySinglePreviewInput(
        direct_session_alias="direct_001", mode="single",
        source={"source_kind": "published", "resource_version_alias": _VERSION_REF}, sample_parameters_json='{"diameter_m":0.2}',
        retain=True,
    )
    response = await application.preview_ifc_geometry(request, request_identity="saved-entry-call")
    assert service.preview_loaded.await_args.kwargs["request_identity"] == "saved-entry-call"
    assert service.preview_loaded.await_args.kwargs["retain_for_inspection"] is True
    assert response.content is None and response.images == ()
    assert response.preview.cases[0].inspection_ref == "cur.inspection_exact"
    assert response.preview.operation_alias == "op_019"
    projected = _application()
    projected.preview_ifc_geometry.return_value = response
    async with Client(_server(projected)) as client:
        result = await client.call_tool("ifc_geometry_preview", request.model_dump(mode="json"))
    assert [item.type for item in result.content] == ["text"]
    assert _preview_json(result)["cases"][0]["diagnostics"][0]["phase"] == "render"


async def test_saved_section_preserves_measurement_indices_and_all_actual_images_without_build() -> None:
    selection, coordinates = InspectionWholeSelection(), InspectionResultCoordinates()
    measurements, images = [], []
    for index, offset in enumerate((0.0, 0.002)):
        measurements.append(GeometrySectionMeasurement(
            section_index=index, offset_m=offset, status="empty", objects=(),
            plane=GeometrySectionResolvedPlane(origin_m=(0, 0, offset), unit_normal=(0, 0, 1),
                u_axis=(1, 0, 0), v_axis=(0, 1, 0)),
        ))
        payload = _PREVIEW_BYTES + bytes([index])
        images.append(GeometrySectionImage(
            section_index=index, preview=GeometryPreview(width_px=64, height_px=64,
                payload=payload, sha256=hashlib.sha256(payload).hexdigest()),
            resolved_display={"u_min_m": -1, "u_max_m": 1, "v_min_m": -1, "v_max_m": 1},
            display_clipped=False,
        ))
    section = GeometrySectionResult(geometry_sha256=_SHA, selection=selection, coordinates=coordinates,
        measurements=tuple(measurements), images=tuple(images))
    application = _GeometryFacadeProbe(SimpleNamespace(identity=object()))
    service = SimpleNamespace(preview=AsyncMock(), preview_inspection=AsyncMock(
        return_value=_inspection_outcome(section=section)))
    application._geometry_preview_service = service
    request = DirectIfcGeometryInspectionPreviewInput.model_validate({
        "direct_session_alias": "direct_001", "mode": "inspection", "inspection_ref": "cur.inspection_exact",
        "selection": selection.model_dump(), "coordinate_space": coordinates.model_dump(),
        "observation": {"observation_kind": "section", "plane": {"origin_m": [0, 0, 0], "normal": [0, 0, 1]}, "offsets_m": [0, 0.002]},
    })
    response = await application.preview_ifc_geometry(request)
    service.preview.assert_not_awaited()
    assert response.preview.operation_alias is None
    assert [item.section_index for item in response.preview.measurements] == [0, 1]
    projected = _application()
    projected.preview_ifc_geometry.return_value = response
    async with Client(_server(projected)) as client:
        result = await client.call_tool("ifc_geometry_preview", request.model_dump(mode="json"))
    assert [item.type for item in result.content] == ["text", "image", "image"]
    assert [base64.b64decode(item.data) for item in result.content[1:]] == [item.preview.payload for item in images]
    assert _preview_json(result)["measurements"][1]["offset_m"] == 0.002


async def test_inspection_query_fresh_repair_page_project_the_same_exact_context() -> None:
    from CAD2BIM_backend.domain.geometry_inspection import InspectionFragmentSelection, InspectionFragmentLocalCoordinates
    from CAD2BIM_backend.domain.geometry_query import GeometryQueryPage, GeometryQueryTargetKind
    from CAD2BIM_mcp.direct.ifc.geometry.contracts import validate_geometry_query_input

    selection = InspectionFragmentSelection(fragment_key="body")
    coordinates = InspectionFragmentLocalCoordinates(fragment_key="body")
    page = GeometryQueryPage(attempt_alias=_ATTEMPT_REF, target_kind=GeometryQueryTargetKind.GEOMETRY_INSPECTION,
        inspection_ref="cur.inspection_exact", selection=selection, coordinate_space=coordinates,
        source_sha256=_SHA, geometry_sha256="b" * 64, result_bytes=32, total_count=2,
        returned_count=1, results=({"area_m2": 0.002},), next_cursor="cur.saved_page")
    application = _GeometryFacadeProbe(SimpleNamespace(identity=object()))
    service = SimpleNamespace(fresh_inspection=AsyncMock(return_value=page),
        fresh=AsyncMock(), resolve_attempt_alias=AsyncMock(return_value="exact-parent-attempt"),
        repair=AsyncMock(return_value=page), continue_page=AsyncMock(return_value=page))
    application._geometry_query_service = service
    requests = (
        {"mode": "fresh", "target": {"target_kind": "geometry_inspection", "inspection_ref": page.inspection_ref,
            "selection": selection.model_dump(), "coordinate_space": coordinates.model_dump()},
            "code": "def query(ctx): return []", "page_size": 1},
        {"mode": "repair", "parent_attempt_alias": _ATTEMPT_REF, "edits": [{"old_text": "return []", "new_text": "return [1]"}], "page_size": 1},
        {"mode": "cursor", "cursor": page.next_cursor},
    )
    targets = []
    for raw in requests:
        request = validate_geometry_query_input({"direct_session_alias": "direct_001", **raw})
        result = await application.query_ifc_geometry_python(request, request_identity="saved-query-action")
        targets.append(result.target.model_dump())
    assert targets == [requests[0]["target"]] * 3
    service.fresh.assert_not_awaited()
    assert service.fresh_inspection.await_args.kwargs["request_identity"] == "saved-query-action"
    assert service.fresh_inspection.await_args.kwargs["selection"] == selection
    assert service.fresh_inspection.await_args.kwargs["coordinate_space"] == coordinates
    assert service.repair.await_args.args[1] == "exact-parent-attempt"
    assert service.continue_page.await_args.args[1] == page.next_cursor








async def test_i0b302_direct_facade_projects_binary_source_members() -> None:
    scope = SimpleNamespace(identity=object())
    application = _GeometryFacadeProbe(scope)
    summary = {
        "format": "indexed_triangle_mesh_v1",
        "unit": "METRE",
        "bounds": {"minimum_m": [0.0, 0.0, 0.0], "maximum_m": [1.0, 1.0, 1.0]},
        "topology": {
            "vertex_count": 3,
            "edge_count": 3,
            "wire_count": 1,
            "face_count": 1,
            "shell_count": 0,
            "solid_count": 0,
            "triangle_count": 1,
            "boundary_edge_count": 3,
        },
        "surface_area_m2": 0.5,
        "volume_m3": None,
    }
    bundle = SimpleNamespace(
        resource=SimpleNamespace(
            resource_id="internal-resource",
            resource_kind=GeometryResourceKind.MAPPED_MODEL,
        ),
        version=SimpleNamespace(
            resource_version_id="internal-version",
            manifest={
                "geometry_summary": summary,
                "geometry_format": "indexed_triangle_mesh_v1",
                "unit": "METRE",
                "local_frame": {"handedness": "right"},
                "canonical_geometry_sha256": _SHA,
                "geometry_inputs": [
                    {
                        "input_key": "body",
                        "resource_id": "input-resource",
                        "resource_version_id": "input-version",
                        "content_digest": _SHA,
                        "canonical_geometry_sha256": _SHA,
                    }
                ],
                "interfaces": [
                    {
                        "interface_key": "inlet",
                        "local_frame": {
                            "translation_m": [0.0, 0.0, 0.0],
                            "rotation": [
                                [1.0, 0.0, 0.0],
                                [0.0, 1.0, 0.0],
                                [0.0, 0.0, 1.0],
                            ],
                        },
                        "profile": {"kind": "circular", "diameter_m": 0.2},
                    }
                ],
            },
            content_digest=_SHA,
        ),
        assets=(
            SimpleNamespace(
                asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY,
                sha256=_SHA,
            ),
        ),
    )
    application._geometry_catalog_service = SimpleNamespace(
        resolve_exact_version=AsyncMock(return_value=bundle),
        version_ref=Mock(return_value="cur.input_version"),
    )
    input_bundle = SimpleNamespace(
        version=SimpleNamespace(content_digest=_SHA),
    )
    application._geometry_resource_service = SimpleNamespace(
        get_exact_version=AsyncMock(return_value=input_bundle)
    )
    application._geometry_source_service = SimpleNamespace(
        read_source=AsyncMock(
            return_value=SimpleNamespace(
                members=(
                    SimpleNamespace(
                        source_path="source.ifc",
                        size_bytes=1024,
                        sha256=_SHA,
                        text_available=False,
                    ),
                )
            )
        )
    )
    now = _results().now
    info = SimpleNamespace(
        resource_root_ref=_RESOURCE_REF,
        resource_kind=GeometryResourceKind.MAPPED_MODEL,
        owner_scope=GeometryResourceScopeKind.SYSTEM,
        display_name="Binary IFC source",
        version=SimpleNamespace(
            resource_version_ref=_VERSION_REF,
            version_no=1,
            origin_kind="system_seed",
            source_available=True,
            preview_available=False,
            created_at=now,
        ),
    )

    result = await application._version_info(scope, info)

    assert result.preview_available is True
    assert result.source_files[0].source_path == "source.ifc"
    assert result.source_files[0].text_available is False
    assert result.interfaces[0].interface_key == "inlet"
    assert result.interfaces[0].local_frame.translation_m == (0.0, 0.0, 0.0)
    assert result.interfaces[0].profile is not None
    assert result.interfaces[0].profile.kind == "circular"
    assert result.geometry_inputs[0].model_dump(mode="json") == {
        "input_key": "body",
        "resource_version_alias": "cur.input_version",
    }
    assert "content_sha256" not in result.model_dump(mode="json")
    application._geometry_resource_service.get_exact_version.assert_awaited_once_with(
        scope.identity,
        resource_id="input-resource",
        resource_version_id="input-version",
    )


async def test_direct_info_reads_parameterized_geometry_without_sample_or_build() -> None:
    scope = SimpleNamespace(identity=object())
    application = _GeometryFacadeProbe(scope)
    schema_bytes = b'{"type":"object"}'
    parameter_asset = SimpleNamespace(
        asset_role=GeometryResourceAssetRole.PARAMETER_SCHEMA,
        sha256=hashlib.sha256(schema_bytes).hexdigest(),
        size_bytes=len(schema_bytes),
    )
    bundle = SimpleNamespace(
        resource=SimpleNamespace(resource_id="geometry", resource_kind=GeometryResourceKind.MODELING_FUNCTION),
        version=SimpleNamespace(
            resource_version_id="version",
            manifest={"unit": "METRE", "local_frame": {"handedness": "right"}},
        ),
        assets=(parameter_asset,),
    )
    application._geometry_catalog_service = SimpleNamespace(resolve_exact_version=AsyncMock(return_value=bundle))
    application._geometry_resource_service = SimpleNamespace(
        read_exact_asset=AsyncMock(return_value=(parameter_asset, schema_bytes)),
        get_exact_version=AsyncMock(),
    )
    application._geometry_source_service = SimpleNamespace(read_source=AsyncMock(return_value=SimpleNamespace(
        members=(SimpleNamespace(source_path="main.py", size_bytes=10, sha256=_SHA, text_available=True),),
    )))
    application._geometry_runtime = Mock()
    now = _results().now
    info = SimpleNamespace(
        resource_root_ref=_RESOURCE_REF,
        resource_kind=GeometryResourceKind.MODELING_FUNCTION,
        owner_scope=GeometryResourceScopeKind.TASK,
        display_name="Parameterized geometry",
        version=SimpleNamespace(resource_version_ref=_VERSION_REF, version_no=1,
            origin_kind="agent_generated", source_available=True, preview_available=True, created_at=now),
    )

    result = await application._version_info(scope, info)

    assert result.resource_kind == GeometryResourceKind.MODELING_FUNCTION
    assert result.parameter_schema_json == schema_bytes.decode()
    assert result.source_files[0].source_path == "main.py"
    assert result.preview_available is True
    assert result.geometry_format is result.bbox is result.topology is None
    application._geometry_runtime.assert_not_called()
    application._geometry_resource_service.get_exact_version.assert_not_awaited()


@pytest.mark.parametrize("kind,asset_sha,manifest_sha", (
    (GeometryResourceKind.MAPPED_MODEL, None, _SHA),
    (GeometryResourceKind.MAPPED_MODEL, _SHA, "b" * 64),
    (GeometryResourceKind.MODELING_FUNCTION, None, _SHA),
    (GeometryResourceKind.MODELING_FUNCTION, _SHA, "b" * 64),
))
def test_info_representative_asset_still_rejects_missing_or_inconsistent_records(kind, asset_sha, manifest_sha) -> None:
    role = (GeometryResourceAssetRole.CANONICAL_GEOMETRY
            if kind is GeometryResourceKind.MAPPED_MODEL else GeometryResourceAssetRole.SAMPLE_OUTPUT)
    manifest_key = "canonical_geometry_sha256" if kind is GeometryResourceKind.MAPPED_MODEL else "sample_output_sha256"
    bundle = SimpleNamespace(
        resource=SimpleNamespace(resource_kind=kind),
        version=SimpleNamespace(manifest={manifest_key: manifest_sha} if manifest_sha else {}),
        assets=() if asset_sha is None else (SimpleNamespace(asset_role=role, sha256=asset_sha),),
    )
    with pytest.raises(ValueError):
        _representative_geometry_sha256(bundle)


async def test_i0b302_geometry_provider_rejects_ambiguous_or_open_inputs() -> None:
    application = _application()
    server = _server(application)
    invalid_calls = (
        (
            "ifc_geometry_resource_list",
            {
                "direct_session_alias": "direct_001",
                "resource_kinds": ["mapped_model"],
                "cursor": "cur.page_2",
            },
        ),
        (
            "ifc_geometry_resource_info",
            {
                "direct_session_alias": "direct_001",
                "target": {
                    "target_kind": "resource_root",
                    "resource_alias": _RESOURCE_REF,
                    "resource_version_alias": _VERSION_REF,
                },
            },
        ),
        (
            "ifc_geometry_resource_source_read",
            {
                "direct_session_alias": "direct_001",
                "resource_version_alias": _VERSION_REF,
                "source_path": "main.py",
                "view_mode": "lines",
                "start_line": 1,
            },
        ),
        (
            "ifc_geometry_query_python",
            {
                "direct_session_alias": "direct_001",
                "mode": "cursor",
                "cursor": "cur.result_2",
                "code": "def query(ctx): return 1",
            },
        ),
        (
            "ifc_geometry_query_python",
            {
                "direct_session_alias": "direct_001",
                "mode": "fresh",
                "resource_version_alias": _VERSION_REF,
                "code": "def query(ctx): return 1",
            },
        ),
        (
            "ifc_geometry_query_python",
            {
                "direct_session_alias": "direct_001",
                "mode": "fresh",
                "target": {
                    "target_kind": "product_realization_preview",
                    "product_realization_preview_capability": "cur.old_preview",
                    "resource_version_alias": _VERSION_REF,
                },
                "code": "def query(ctx): return 1",
            },
        ),
        (
            "ifc_geometry_query_python",
            {
                "direct_session_alias": "direct_001",
                "mode": "repair",
                "parent_attempt_alias": _ATTEMPT_REF,

                "edits": [{"old_text": "return 1", "new_text": "return 2"}],
                "target": {
                    "target_kind": "geometry_resource_version",
                    "resource_version_alias": _VERSION_REF,
                },
            },
        ),
        (
            "ifc_geometry_preview",
            {
                "direct_session_alias": "direct_001",
                "resource_version_alias": _VERSION_REF,
                "parameters": [{"name": "diameter", "value_kind": "number", "value": 0.1}],
            },
        ),
    )

    async with Client(server) as client:
        for name, payload in invalid_calls:
            with pytest.raises(ToolError):
                await client.call_tool(name, payload)

    application.list_ifc_geometry_resources.assert_not_awaited()
    application.get_ifc_geometry_resource_info.assert_not_awaited()
    application.read_ifc_geometry_resource_source.assert_not_awaited()
    application.query_ifc_geometry_python.assert_not_awaited()
    application.preview_ifc_geometry.assert_not_awaited()
