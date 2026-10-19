from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.geometry_catalog import ProductRealizationPreviewAuthorization
from CAD2BIM_backend.domain.geometry_inspection import GeometryInspectionDiagnostic
from CAD2BIM_backend.domain.geometry_interface import (
    CircularInterfaceProfile,
    InterfaceFrameEvaluation,
    InterfaceFrameRelation,
    RectangleInterfaceProfile,
    evaluate_interface_frame_relation,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometryFormat,
    GeometryBounds,
    GeometryDisplayQualityWarning,
    GeometryRenderCoordinateSpace,
    GeometryRenderInterfaceMarkerKind,
    GeometryRenderInterfaceOverlay,
    GeometryRenderedPreviewMetadata,
    GeometryResolvedOrthographicCamera,
    GeometryResolvedPreviewPanel,
    GeometryStoredPreviewMetadata,
    GeometrySummary,
    GeometryTopologySummary,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.product_realization import (
    AmbiguousProductRealizationProposal,
    InfeasibleProductRealizationProposal,
    ProductGeometryInstanceReceipt,
    ProductRealizationAmbiguity,
    ProductRealizationComponentPlacement,
    ProductRealizationComponentWorldTransform,
    ProductRealizationFragmentPlacement,
    ProductRealizationFrameDiagnostic,
    ProductRealizationInvocation,
    ProductRealizationExecutionPolicy,
    ProductRealizationResult,
    ProductRealizationStatus,
    ProductRealizationInterfaceWorldFrame,
    ProductRealizationViolation,
    RigidTransform3D,
    SolvedProductRealizationProposal,
)
from CAD2BIM_backend.domain.product_family import ProductFamilyModelInterfaceSource
from CAD2BIM_backend.domain.product_realization_preview import (
    ProductRealizationPreviewError,
    ProductRealizationPreviewErrorCode,
    ProductRealizationPreviewOutcome,
)
from CAD2BIM_mcp.direct.ifc.product_family.preview_application import (
    IfcProductRealizationPreviewApplicationMixin,
)
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import (
    DirectIfcProductRealizationPreviewProviderInput,
    IfcProductRealizationAmbiguousResult,
    IfcProductRealizationPreviewResult,
    validate_product_realization_preview_input,
    validate_product_realization_preview_provider_input,
)
from CAD2BIM_mcp.direct.ifc.product_family.preview_tools import (
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS,
)
from CAD2BIM_mcp.direct.tool_support import ProviderJsonDocumentError
from CAD2BIM_mcp.direct.provider_schema import provider_output_schema
from CAD2BIM_mcp.direct.references import reference_schema_projection
from CAD2BIM_mcp.tool_docs import ifc_product_family as _family_tool_docs


_FAMILY_VERSION_ALIAS = "cur.family_version_ref_001"
_SHA = "a" * 64
_OTHER_SHA = "b" * 64
_PNG = b"\x89PNG\r\n\x1a\nproduct-realization"
_FORBIDDEN_SCHEMA_KEYS = {"allOf", "if", "then", "else", "not"}
_INTERNAL_NAMES = {
    "model_id",
    "revision_id",
    "artifact_id",
    "artifact_version_id",
    "family_version_id",
    "tenant_id",
    "task_id",
    "run_id",
    "storage_key",
    "storage_path",
}


def _preview_json(response):
    assert response.structured_content is None
    assert response.content[0].type == "text"
    return json.loads(response.content[0].text)


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def _assert_provider_safe_schema(schema: Mapping[str, object]) -> None:
    Draft202012Validator.check_schema(schema)
    for node in _walk(schema):
        if isinstance(node, Mapping):
            assert node != {}
            assert node.get("type") != "unknown"
            assert not (_FORBIDDEN_SCHEMA_KEYS & set(node))
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False
    serialized = json.dumps(schema, sort_keys=True).lower()
    assert '"additionalproperties": {}' not in serialized
    for name in _INTERNAL_NAMES:
        assert f'"{name}"' not in serialized


def _scope() -> InvocationScope:
    return InvocationScope(
        identity=TenantExecutionContext(
            tenant_id="tenant-preview",
            principal_id="principal-preview",
            task_id="task-preview",
            actor_kind=TenantActorKind.USER,
            command_id="command-preview",
        ),
        owner_id="tenant-preview",
        run_id="run-preview",
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=4,
    )


def _summary() -> GeometrySummary:
    return GeometrySummary(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        bounds=GeometryBounds(minimum_m=(0.0, 0.0, 0.0), maximum_m=(1.0, 1.0, 1.0)),
        topology=GeometryTopologySummary(
            vertex_count=8,
            edge_count=12,
            wire_count=6,
            face_count=6,
            shell_count=1,
            solid_count=1,
            triangle_count=0,
            boundary_edge_count=0,
        ),
        surface_area_m2=6.0,
        volume_m3=1.0,
    )


def _realization(status: ProductRealizationStatus) -> ProductRealizationResult:
    common = {
        "invocation": ProductRealizationInvocation(
            product_origin=(0, 0, 0), length_unit={"name": "METRE"},
            public_parameters={"enabled": True, "size": 2},
        ),
        "policy": ProductRealizationExecutionPolicy(),
        "family_contract_digest": _SHA,
        "binding_digest": _OTHER_SHA,
        "source_bundle_sha256": "c" * 64,
        "normalized_public_parameters": {"enabled": True, "size": 2},
    }
    if status is ProductRealizationStatus.SOLVED:
        return ProductRealizationResult(
            **common,
            proposal=SolvedProductRealizationProposal(
                internal_parameters={"offset_m": 0.25},
                components=(ProductRealizationComponentPlacement(component_key="body"),),
                fragments=(
                    ProductRealizationFragmentPlacement(
                        fragment_key="body_shape", binding_key="body_geometry",
                        geometry_parameters={"radius_m": 0.1},
                    ),
                ),
            ),
            instance_receipts=(
                ProductGeometryInstanceReceipt(
                    fragment_key="body_shape",
                    binding_key="body_geometry",
                    normalized_parameters={"radius_m": 0.1},
                    geometry_sha256="e" * 64,
                    geometry_summary=_summary(),
                ),
            ),
            component_project_world_transforms=(
                ProductRealizationComponentWorldTransform(
                    component_key="body",
                    project_world_transform=RigidTransform3D(),
                ),
            ),
        )
    if status is ProductRealizationStatus.AMBIGUOUS:
        proposal = AmbiguousProductRealizationProposal(
            ambiguities=(
                ProductRealizationAmbiguity(
                    ambiguity_key="clocking",
                    message="Two exact orientations remain.",
                ),
            )
        )
    else:
        proposal = InfeasibleProductRealizationProposal(
            violations=(
                ProductRealizationViolation(
                    violation_key="clearance",
                    message="The requested clearance is infeasible.",
                ),
            )
        )
    return ProductRealizationResult(**common, proposal=proposal)


def _outcome(status: ProductRealizationStatus) -> ProductRealizationPreviewOutcome:
    realization = _realization(status)
    if status is not ProductRealizationStatus.SOLVED:
        return ProductRealizationPreviewOutcome(realization=realization)
    return ProductRealizationPreviewOutcome(
        realization=realization,
        preview=ProductRealizationPreviewAuthorization(
            family_version_ref=_FAMILY_VERSION_ALIAS,
            title="Exact regulator family",
            size_bytes=len(_PNG),
            content_sha256=hashlib.sha256(_PNG).hexdigest(),
            content=_PNG,
        ),
    )


def _preview_authorization() -> ProductRealizationPreviewAuthorization:
    return ProductRealizationPreviewAuthorization(
        family_version_ref=_FAMILY_VERSION_ALIAS,
        title="Exact regulator family",
        size_bytes=len(_PNG),
        content_sha256=hashlib.sha256(_PNG).hexdigest(),
        content=_PNG,
        render_metadata=GeometryRenderedPreviewMetadata(
            scene_sha256="1" * 64,
            coordinate_space=GeometryRenderCoordinateSpace.PROJECT_WORLD,
            connection_context={"mode": "pipes", "items": [{"start_m": [0, 0, 0],
                "end_m": [0, 0, 0.1], "outer_diameter_m": 0.05}]},
            connection_context_diagnostics=("Context unavailable for interface 'mount': no outer diameter.",),
            display_quality_warnings=(GeometryDisplayQualityWarning(
                geometry_sha256="2" * 64, face_index=0, native_area_m2=1.0,
                selected_attempt="initial",
            ),),
            resolved_panels=(GeometryResolvedPreviewPanel(
                panel_key="front", x_px=0, y_px=0, width_px=640, height_px=480,
                geometry_clipped=True, visible_overlay_ids=("family:inlet",),
                camera=GeometryResolvedOrthographicCamera(
                    target_m=(0.0, 0.0, 0.0), direction=(0.0, 1.0, 0.0),
                    up=(0.0, 0.0, 1.0), right=(1.0, 0.0, 0.0),
                    horizontal_extent_m=2.0, vertical_extent_m=1.5,
                ),
            ),),
        ),
        interface_overlays=(GeometryRenderInterfaceOverlay(
            overlay_id="family:inlet", label="inlet",
            marker_kind=GeometryRenderInterfaceMarkerKind.FAMILY_EXTERNAL,
            frame=RigidTransform3D(),
        ),),
    )


class _Application(IfcProductRealizationPreviewApplicationMixin):
    def __init__(self, outcome: ProductRealizationPreviewOutcome) -> None:
        self._scope_binder = Mock(return_value=nullcontext())
        self._product_family_catalog_service = SimpleNamespace(
            resolve_exact_version=AsyncMock(return_value=SimpleNamespace(version="exact"))
        )
        self._product_realization_preview_service = SimpleNamespace(
            preview=AsyncMock(return_value=outcome),
            current_ifc_invocation_scope=lambda **kwargs: nullcontext(),
        )

    async def _run_runtime_read(self, _alias, _action, operation, **_options):
        return await operation(_scope())


@pytest.mark.parametrize("input_source", ["cases_json", "invocation_json"])
async def test_draft_preview_input_failure_preserves_domain_diagnostic(input_source):
    from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringApplicationError
    diagnostic = {"item_index":0, "field":"public_parameters", "reason":"PUBLIC_PARAMETER_INVALID",
                  "correction":"Supply required public parameters."}
    application = _Application(None)
    operation = AsyncMock(side_effect=ProductFamilyAuthoringApplicationError(
        "PRODUCT_FAMILY_INPUT_INVALID", "Missing required parameter: diameter", diagnostic=diagnostic))
    with pytest.raises(DirectApplicationError) as rejected:
        await application._product_realization_preview_read("direct_001", operation, input_source=input_source)
    assert rejected.value.code == "PRODUCT_FAMILY_INPUT_INVALID"
    assert rejected.value.phase == "preflight" and rejected.value.diagnostic == diagnostic
    assert "diameter" in str(rejected.value)
    with pytest.raises(DirectApplicationError) as unknown:
        await application._product_realization_preview_read(
            "direct_001", AsyncMock(side_effect=ValueError("unclassified failure")), input_source=input_source)
    assert unknown.value.code == "PRODUCT_REALIZATION_PREVIEW_INTEGRITY_ERROR"
    assert unknown.value.phase == "output_validation"


def _self_geometry_input() -> dict[str, object]:
    return {"mode": "single",
        "direct_session_alias": "direct_001",
        "attachment_kind": "self_geometry",
        "product_origin": [0, 0, 0],
        "length_unit": {"name": "METRE"},
        "source": {"source_kind": "published", "family_version_alias": _FAMILY_VERSION_ALIAS},
        "public_parameters": {"enabled": True, "size": 2},
    }




def _self_geometry_provider_request(kind="fixed_point"):
    condition = {"kind": kind, "outer_diameter_m": 0.2, "direction_relation": "opposed",
                 "reference_direction": [2, 0, 0], "position_tolerance_m": 0.001,
                 "angular_tolerance_rad": 0.001}
    if kind == "finite_segment":
        condition.update(start_m=[3, 4, 4], end_m=[3, 4, 6])
    else:
        condition.update(point_m=[3, 4, 5], axis=[0, 0, 2])
    return {"mode": "single",
        "direct_session_alias": "direct_001", "attachment_kind": "self_geometry",
        "source": {"source_kind": "published", "family_version_alias": _FAMILY_VERSION_ALIAS},
        "invocation_json": json.dumps({
            "product_origin": [1000, 2000, 3000],
            "length_unit": {"name": "METRE", "prefix": "MILLI"},
            "public_parameters": {
                "points": [[0, 0, 0], [1, 2, 3]],
                "section": {"diameter": 0.2},
            },
            "external_constraints": [{"interface_key": "outlet", "source_kind": "design_target",
                "condition": condition}],
        }),
    }


@pytest.mark.parametrize("kind", ["finite_segment", "fixed_point", "infinite_axis"])
@pytest.mark.asyncio
async def test_self_geometry_provider_preserves_installation_conditions_and_structured_values(kind):
    request = validate_product_realization_preview_provider_input(_self_geometry_provider_request(kind))
    condition = request.external_constraints[0].condition
    assert condition.kind == kind
    if kind != "finite_segment":
        assert condition.axis == (0, 0, 1)
    else:
        assert condition.start_m == (3, 4, 4) and condition.end_m == (3, 4, 6)
    assert condition.reference_direction == (1, 0, 0)
    application = _Application(_outcome(ProductRealizationStatus.AMBIGUOUS))
    await application.preview_ifc_product_realization(request)
    invocation = application._product_realization_preview_service.preview.await_args.kwargs["invocation"]
    assert invocation.realization_kind == "self_geometry"
    assert invocation.external_constraints == request.external_constraints
    assert invocation.public_parameters == {"points": [[0, 0, 0], [1, 2, 3]], "section": {"diameter": 0.2}}
    assert "attachments" not in invocation.model_dump()
    assert "interface_bindings" not in invocation.model_dump()


@pytest.mark.parametrize("attachment_kind", ["self_geometry", "standard_model"])
@pytest.mark.asyncio
async def test_preview_parameter_map_preserves_complete_json_values(attachment_kind):
    values = {
        "enabled": True,
        "size": 2,
        "diameter_m": 0.2,
        "variant": "round",
        "points": [[0, 0, 0], [1, 2, 3]],
        "section": {"kind": "circular", "diameter_m": 0.2, "optional": None},
    }
    provider = _provider_input(attachment_kind=attachment_kind)
    document = json.loads(provider["invocation_json"])
    document["public_parameters"] = values
    provider["invocation_json"] = json.dumps(document)
    request = validate_product_realization_preview_provider_input(provider)
    application = _Application(_outcome(ProductRealizationStatus.AMBIGUOUS))
    await application.preview_ifc_product_realization(request)

    invocation = application._product_realization_preview_service.preview.await_args.kwargs["invocation"]
    assert invocation.realization_kind == attachment_kind
    assert invocation.public_parameters == values
    assert type(invocation.public_parameters["enabled"]) is bool
    assert type(invocation.public_parameters["size"]) is int
    assert type(invocation.public_parameters["diameter_m"]) is float


@pytest.mark.parametrize("attachment_kind", ["self_geometry", "standard_model"])
@pytest.mark.parametrize(
    "values",
    [
        [{"parameter_kind": "integer", "parameter_key": "size", "value": 2}],
        {"Invalid_key": 2},
        {"x" * 65: 2},
        {f"key_{index}": index for index in range(65)},
    ],
    ids=["retired_list", "invalid_key", "overlong_key", "parameter_count"],
)
def test_preview_parameter_map_rejects_retired_shape_and_key_budget(attachment_kind, values):
    provider = _provider_input(attachment_kind=attachment_kind)
    document = json.loads(provider["invocation_json"])
    document["public_parameters"] = values
    provider["invocation_json"] = json.dumps(document)
    with pytest.raises(ProviderJsonDocumentError):
        validate_product_realization_preview_provider_input(provider)
    with pytest.raises(ValidationError):
        validate_product_realization_preview_input({
            **_self_geometry_input(),
            "attachment_kind": attachment_kind,
            "public_parameters": values,
        })


@pytest.mark.parametrize("attachment_kind", ["self_geometry", "standard_model"])
@pytest.mark.parametrize(
    "parameter_json, reason",
    [
        ('{"size":1,"size":2}', "duplicate object key"),
        ('{"section":{"diameter":0.1,"diameter":0.2}}', "duplicate object key"),
        ('{"diameter_m":NaN}', "non-finite number"),
        ('{"section":{"diameter_m":1e309}}', "non-finite number"),
    ],
)
def test_preview_parameter_map_keeps_bounded_json_decoder_rejections(
    attachment_kind, parameter_json, reason,
):
    provider = _provider_input(attachment_kind=attachment_kind)
    document = json.loads(provider["invocation_json"])
    document.pop("public_parameters")
    provider["invocation_json"] = (
        json.dumps(document)[:-1] + ',"public_parameters":' + parameter_json + "}"
    )
    with pytest.raises(ProviderJsonDocumentError, match=reason):
        validate_product_realization_preview_provider_input(provider)


@pytest.mark.parametrize("attachment_kind", ["self_geometry", "standard_model"])
def test_preview_omitted_parameter_map_stays_empty_for_domain_defaults(attachment_kind):
    provider = _provider_input(attachment_kind=attachment_kind)
    document = json.loads(provider["invocation_json"])
    document.pop("public_parameters")
    provider["invocation_json"] = json.dumps(document)
    assert validate_product_realization_preview_provider_input(provider).public_parameters == {}
    document["public_parameters"] = {}
    provider["invocation_json"] = json.dumps(document)
    assert validate_product_realization_preview_provider_input(provider).public_parameters == {}


@pytest.mark.parametrize("attachment_kind", ["self_geometry", "standard_model"])
def test_preview_invocation_rejects_manual_schema_marker(attachment_kind):
    provider = _provider_input(attachment_kind=attachment_kind)
    document = json.loads(provider["invocation_json"])
    assert "schema" not in document
    assert validate_product_realization_preview_provider_input(provider).product_origin == (0.0, 0.0, 0.0)
    document["schema"] = "retired-marker"
    provider["invocation_json"] = json.dumps(document)
    with pytest.raises(ProviderJsonDocumentError, match="invalid_path=schema; error_type=extra_forbidden"):
        validate_product_realization_preview_provider_input(provider)


@pytest.mark.parametrize("mutation", ["missing_origin", "observation_without_evidence", "unknown_condition_field", "missing_axis", "retired_condition"])
def test_self_geometry_provider_rejects_incomplete_or_open_conditions(mutation):
    request = _self_geometry_provider_request()
    document = json.loads(request["invocation_json"])
    requirement = document["external_constraints"][0]
    if mutation == "missing_origin":
        document.pop("product_origin")
    elif mutation == "observation_without_evidence":
        requirement["source_kind"] = "observed_interface"
    elif mutation == "unknown_condition_field":
        requirement["condition"]["guessed_contact"] = True
    elif mutation == "missing_axis":
        requirement["condition"].pop("axis")
    else:
        requirement["condition"] = {"position": {"position_kind": "point", "point_m": [3, 4, 5]},
            "direction": {"direction_kind": "opposed", "axis": [0, 0, 1]},
            "roll": {"roll_kind": "none"}, "profile": {"profile_kind": "none"}}
    request["invocation_json"] = json.dumps(document)
    with pytest.raises(ProviderJsonDocumentError):
        validate_product_realization_preview_provider_input(request)


def test_structured_parameter_result_is_closed_json_transport_and_reusable_after_decode():
    from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import product_realization_parameter_values
    values = {"points": [[0, 0, 0], [1, 2, 3]], "section": {"diameter": 0.2}}
    projected = product_realization_parameter_values(values)
    assert {item.parameter_key: json.loads(item.value_json) for item in projected} == values
    assert [item.parameter_kind for item in projected] == ["array", "object"]
    for item in projected:
        _assert_provider_safe_schema(type(item).model_json_schema())


def _provider_input(
    *,
    attachment_kind: str = "self_geometry",
    product_origin: list[float] | None = None,
    length_unit: dict[str, object] | None = None,
    connection_interfaces: list[dict[str, object]] | None = None,
    host_interfaces: list[dict[str, object]] | None = None,
    endpoints: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    invocation: dict[str, object] = {
        "public_parameters": _self_geometry_input()["public_parameters"],
        "product_origin": product_origin if product_origin is not None else [0, 0, 0],
        "length_unit": length_unit if length_unit is not None else {"name": "METRE"},
    }
    if connection_interfaces is not None:
        invocation["connection_interfaces"] = connection_interfaces
    if host_interfaces is not None:
        invocation["host_interfaces"] = host_interfaces
    if endpoints is not None:
        invocation["endpoints"] = endpoints
    return {"mode": "single",
        "direct_session_alias": "direct_001",
        "attachment_kind": attachment_kind,
        "source": {"source_kind": "published", "family_version_alias": _FAMILY_VERSION_ALIAS},
        "invocation_json": json.dumps(invocation, separators=(",", ":"), sort_keys=True),
    }




async def test_product_preview_outer_boundary_covers_queue_execution_and_publication() -> None:
    application = IfcProductRealizationPreviewApplicationMixin()
    run_read = AsyncMock(return_value="preview-result")
    application._run_runtime_read = run_read
    operation = AsyncMock()

    assert (
        await application._product_realization_preview_read("direct_001", operation)
        == "preview-result"
    )
    assert run_read.await_args.args == (
        "direct_001",
        "ifc_product_realization_preview",
        operation,
    )
    assert run_read.await_args.kwargs == {
        "timeout_seconds": 870.0,
        "timeout_code": "PRODUCT_REALIZATION_PREVIEW_TIMEOUT",
        "timeout_message": "The Product Realization preview read boundary expired",
    }


async def test_product_preview_timeout_cancels_the_read_and_preserves_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import CAD2BIM_mcp.direct.ifc.product_family.preview_application as preview_application

    monkeypatch.setattr(preview_application, "_PREVIEW_TIMEOUT_SECONDS", 0.01)
    scope = _scope()
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=scope))
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def block(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    preview = SimpleNamespace(preview=AsyncMock(side_effect=block), inspect=AsyncMock())
    catalog = SimpleNamespace(
        resolve_exact_version=AsyncMock(return_value=SimpleNamespace(version="exact"))
    )
    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        resolver,
        lambda _identity: nullcontext(),
        product_family_catalog_service=catalog,
        product_realization_preview_service=preview,
    )

    with pytest.raises(DirectApplicationError) as caught:
        await facade.preview_ifc_product_realization(
            validate_product_realization_preview_input(_self_geometry_input())
        )

    assert started.is_set()
    assert cancelled.is_set()
    assert caught.value.code == "PRODUCT_REALIZATION_PREVIEW_TIMEOUT"
    assert caught.value.phase == "read"
    assert caught.value.retryable is False
    assert str(caught.value) == "The Product Realization preview read boundary expired"
    preview.preview.assert_awaited_once()





def test_preview_provider_contracts_are_closed_and_hide_internal_identity() -> None:
    _assert_provider_safe_schema(
        TypeAdapter(DirectIfcProductRealizationPreviewProviderInput).json_schema(mode="validation")
    )
    _assert_provider_safe_schema(
        TypeAdapter(IfcProductRealizationPreviewResult).json_schema(mode="serialization")
    )

    with pytest.raises(ValidationError):
        validate_product_realization_preview_input(
            {
                **_self_geometry_input(),
                "attachment_kind": "current_ifc",
                "endpoints": [
                    {
                        "product_global_id": "0BBBBBBBBBBBBBBBBBBBBB",
                        "endpoint_selector": {
                            "boundary": "axis_start",
                            "interface_key": "inlet",
                            "attachment_key": "side_a",
                            "revision_id": "caller-selected-revision",
                        },
                    }
                ],
            }
        )

    with pytest.raises(ValidationError):
        validate_product_realization_preview_input(
            {
                **_self_geometry_input(),
                "family_version_alias": "cur.family_root_001",
                "unexpected": "field",
            }
        )


@pytest.mark.asyncio
async def test_preview_registered_input_schema_stays_below_codex_compaction_budget() -> None:
    application = _Application(_outcome(ProductRealizationStatus.AMBIGUOUS))
    server = FastMCP("product-realization-preview-schema")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)

    tool = (await server.list_tools())[0]
    assert tool.output_schema is None
    resources = {str(item.uri): item for item in await server.list_resources()}
    self_uri = "cad2bim://contracts/ifc_product_realization_preview/self_geometry"
    standard_uri = "cad2bim://contracts/ifc_product_realization_preview/standard_model"
    assert (
        resources[self_uri].description
        == _family_tool_docs.PRODUCT_REALIZATION_SELF_GEOMETRY_INVOCATION_RESOURCE_DESCRIPTION
    )
    assert (
        resources[standard_uri].description
        == _family_tool_docs.PRODUCT_REALIZATION_STANDARD_MODEL_INVOCATION_RESOURCE_DESCRIPTION
    )
    schema_contents = await server.read_resource(
        "cad2bim://contracts/ifc_product_realization_preview/result"
    )
    self_contents = await server.read_resource(self_uri)
    standard_contents = await server.read_resource(standard_uri)
    result_guide = json.loads(schema_contents.contents[0].content)
    assert "$defs" not in result_guide
    assert "SOLVED" in result_guide["single"]["status"]
    output_schema = reference_schema_projection(provider_output_schema(
        IfcProductRealizationPreviewResult, compact=True, preserve_references=True,
    ))[0]
    self_schema = json.loads(self_contents.contents[0].content)
    standard_schema = json.loads(standard_contents.contents[0].content)
    assert tuple(self_schema["properties"]) == (
        "public_parameters",
        "product_origin",
        "length_unit",
        "external_constraints",
    )
    assert tuple(standard_schema["properties"]) == (
        "public_parameters",
        "product_origin",
        "length_unit",
    )
    assert {"product_origin", "length_unit"}.issubset(self_schema["required"])
    assert {"product_origin", "length_unit"}.issubset(standard_schema["required"])
    assert "external_constraints" not in standard_schema["properties"]
    installation_branches = [
        node
        for node in self_schema["$defs"].values()
        if node.get("properties", {}).get("kind", {}).get("const")
        in {"finite_segment", "fixed_point", "infinite_axis"}
    ]
    assert len(installation_branches) == 6
    assert {branch["properties"]["kind"]["const"] for branch in installation_branches} == {"finite_segment", "fixed_point", "infinite_axis"}
    for branch in installation_branches:
        kind = branch["properties"]["kind"]["const"]
        assert "kind" in branch["required"]
        assert branch["additionalProperties"] is False
        assert {"position", "direction", "roll", "profile"}.isdisjoint(branch["properties"])
        for field in ("position_tolerance_m", "angular_tolerance_rad"):
            assert branch["properties"][field]["default"] == 1e-6
        if kind == "fixed_point":
            assert {"point_m", "direction_relation"} <= set(branch["required"])
            if branch["properties"]["direction_relation"].get("const") == "none":
                assert {"axis", "reference_direction"}.isdisjoint(branch["properties"])
            else:
                assert "axis" in branch["required"]
        else:
            assert "direction_relation" in branch["required"]
        assert "outer_diameter_m" not in branch["required"]
        if branch["properties"]["direction_relation"].get("const") == "none":
            assert "reference_direction" not in branch["properties"]
        _assert_provider_safe_schema(branch)
    for kind in ("finite_segment", "fixed_point", "infinite_axis"):
        document = json.loads(_self_geometry_provider_request(kind)["invocation_json"])
        Draft202012Validator(self_schema).validate(document)
    for schema in (self_schema, standard_schema):
        parameters = schema["properties"]["public_parameters"]
        assert parameters["type"] == "object"
        assert parameters["maxProperties"] == 64
        assert parameters["additionalProperties"] is False
        assert "public_parameters" not in schema["required"]
        validator = Draft202012Validator({
            "$defs": schema["$defs"],
            **parameters,
        })
        assert not list(validator.iter_errors(_self_geometry_input()["public_parameters"]))
        assert list(validator.iter_errors({"Invalid_key": 2}))
    assert all(
        "title" not in node
        for schema in (self_schema, standard_schema)
        for node in _walk(schema)
        if isinstance(node, Mapping)
    )
    from tests.unit.mcp.test_provider_output_compaction import _expand_shared_input
    input_branches = _expand_shared_input(tool.parameters)["oneOf"]
    single = input_branches[0]
    assert [item["properties"]["mode"]["const"] for item in input_branches] == ["single", "cases"]
    assert set(single["properties"]) == {"reference_geometries", "direct_session_alias", "mode", "attachment_kind",
        "source", "invocation_json", "preview_profile_json", "retain"}
    assert "mode" in single["required"]
    assert next(item for item in single["properties"]["preview_profile_json"]["anyOf"] if item.get("type") == "string")["maxLength"] == 65_536
    output_branches = output_schema["properties"]["result"]["anyOf"]
    resolved_output_branches = [
        output_schema["$defs"][branch["$ref"].rsplit("/", 1)[-1]]
        if "$ref" in branch
        else branch
        for branch in output_branches
    ]
    assert [branch["properties"]["status"]["const"] for branch in resolved_output_branches if "status" in branch["properties"]] == [
        "SOLVED",
        "SOLVED",
        "AMBIGUOUS",
        "INFEASIBLE",
    ]
    solved_output = resolved_output_branches[0]
    assert "product_realization_preview_capability" not in solved_output["properties"]
    assert "render_metadata" not in solved_output["properties"]
    assert "preview_content_sha256" not in solved_output["properties"]
    assert "host_overlays" not in solved_output["properties"]
    assert "straight_profile_overlays" not in solved_output["properties"]
    assert "IfcStoredPreviewPresentationResult" not in output_schema["$defs"]
    assert "GeometryRenderHostOverlay" not in output_schema["$defs"]
    assert "GeometryRenderStraightProfileOverlay" not in output_schema["$defs"]
    evaluations = [node for node in _walk(output_schema)
                   if isinstance(node, Mapping) and {"profile_satisfied", "origin_distance_m",
                       "direction_residual_rad"} <= set(node.get("properties", {}))]
    assert evaluations
    assert all("profile_satisfied" not in node["required"] for node in evaluations)
    _assert_provider_safe_schema(tool.parameters)
    assert all(
        "title" not in node
        for node in _walk(tool.parameters)
        if isinstance(node, Mapping)
    )
    _assert_provider_safe_schema(output_schema)
    declaration = json.dumps({"input": tool.parameters, "output": output_schema, "description": tool.description})
    assert "product_realization_preview_invocation_v1" not in declaration
    assert single["properties"]["attachment_kind"]["enum"] == ["self_geometry", "standard_model"]
    assert "inspection_ref" in tool.description
    assert "Geometry Preview/Query" in tool.description
    assert "render_json" in tool.description
    assert "product_realization_preview_capability" not in declaration
    assert "cad2bim://contracts/ifc_product_realization_preview/result" in declaration
    from CAD2BIM_mcp.direct.server import create_direct_mcp
    from CAD2BIM_mcp.tool_docs.common import COMMON_PREVIEW_RESOURCE_URI
    assert COMMON_PREVIEW_RESOURCE_URI in tool.description
    composed = create_direct_mcp(Mock())
    common_preview = await composed.read_resource(COMMON_PREVIEW_RESOURCE_URI)
    preview_guide = json.loads(common_preview.contents[0].content)
    assert "width_px" in preview_guide["preview_profile"]
    assert "height_px" in preview_guide["preview_profile"]
    assert "camera" in preview_guide["preview_profile"]
    assert "SOLVED" in preview_guide["preview_results"]









@pytest.mark.asyncio
@pytest.mark.parametrize("attachment_kind", ["standalone", "explicit_connection_interfaces", "explicit_host_interfaces", "current_ifc"])
async def test_removed_host_branches_fail_before_application(attachment_kind):
    application = _Application(_outcome(ProductRealizationStatus.SOLVED))
    server = FastMCP("product-preview-removed-host-input")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)
    async with Client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool("ifc_product_realization_preview",
                                   _provider_input(attachment_kind=attachment_kind))
    application._product_realization_preview_service.preview.assert_not_awaited()





@pytest.mark.parametrize("temporary_interfaces", [None, [], [{"interface_key": "bad"}]])
def test_p4_product_preview_rejects_every_temporary_interface_override(
    temporary_interfaces,
) -> None:
    profile = json.dumps(
        {
            "presentation": {
                "panels": [
                    {
                        "panel_key": "front",
                        "camera": {"camera_kind": "preset_fit", "view": "front"},
                    }
                ],
                "overlay": {
                    "overlay_kind": "interfaces",
                    "temporary_interfaces": temporary_interfaces,
                },
            }
        },
        separators=(",", ":"),
    )

    with pytest.raises(ProviderJsonDocumentError, match="temporary_interfaces"):
        validate_product_realization_preview_provider_input(
            {
                **_provider_input(),
                "preview_profile_json": profile,
            }
        )


@pytest.mark.asyncio
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
        '"overlay":{"overlay_kind":"interfaces","focus":{"focus_kind":"all",'
        '"bad":"\\ud800"}}}}',
    ],
)
async def test_p4_product_preview_profile_failures_stop_before_application(
    preview_profile_json: str,
) -> None:
    application = _Application(_outcome(ProductRealizationStatus.SOLVED))
    server = FastMCP("product-realization-profile-preflight")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(
                "ifc_product_realization_preview",
                {
                    **_provider_input(),
                    "preview_profile_json": preview_profile_json,
                },
            )

    assert "INVALID_ARGUMENT" in str(caught.value)
    application._product_realization_preview_service.preview.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attachment_kind,invocation_json",
    [
        ("self_geometry", "[]"),
        (
            "self_geometry",
            '{"public_parameters":{},"public_parameters":{}}',
        ),
        ("self_geometry", '{"schema":"wrong_product_realization_invocation"}'),
        (
            "self_geometry",
            '{"endpoints":[]}',
        ),
        (
            "explicit_connection_interfaces",
            '{}',
        ),
        (
            "self_geometry",
            '{"public_parameters":NaN}',
        ),
        (
            "self_geometry",
            '{"public_parameters":1e309}',
        ),
        (
            "self_geometry",
            '{"public_parameters":["\\ud800"]}',
        ),
    ],
)
async def test_preview_provider_document_failures_stop_before_application(
    attachment_kind: str,
    invocation_json: str,
) -> None:
    application = _Application(_outcome(ProductRealizationStatus.SOLVED))
    server = FastMCP("product-realization-preview-preflight")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)
    request = {"mode": "single",
        "direct_session_alias": "direct_001",
        "attachment_kind": attachment_kind,
        "source": {"source_kind": "published", "family_version_alias": _FAMILY_VERSION_ALIAS},
        "invocation_json": invocation_json,
    }

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("ifc_product_realization_preview", request)

    message = str(caught.value)
    assert "INVALID_ARGUMENT" in message
    assert "phase=preflight" in message
    assert len(message.encode("utf-8")) <= 2_000
    application._product_realization_preview_service.preview.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_validation_path_does_not_echo_untrusted_extra_key() -> None:
    application = _Application(_outcome(ProductRealizationStatus.SOLVED))
    server = FastMCP("product-realization-preview-safe-path")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)
    oversized_key = "untrusted" * 1_250
    invocation = json.loads(str(_provider_input()["invocation_json"]))
    invocation[oversized_key] = True

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(
                "ifc_product_realization_preview",
                {"mode": "single",
                    "direct_session_alias": "direct_001",
                    "attachment_kind": "self_geometry",
                    "source": {"source_kind": "published", "family_version_alias": _FAMILY_VERSION_ALIAS},
                    "invocation_json": json.dumps(invocation),
                },
            )

    message = str(caught.value)
    assert "INVALID_ARGUMENT" in message
    assert "phase=preflight" in message
    assert oversized_key not in message
    assert len(message.encode("utf-8")) <= 2_000
    application._product_realization_preview_service.preview.assert_not_awaited()


@pytest.mark.asyncio
async def test_application_projects_self_geometry_and_non_solved_without_preview() -> None:
    application = _Application(_outcome(ProductRealizationStatus.AMBIGUOUS))
    request = validate_product_realization_preview_input(_self_geometry_input())

    authorization = await application.preview_ifc_product_realization(request)

    assert isinstance(authorization.result.result, IfcProductRealizationAmbiguousResult)
    assert authorization.result.result.status == "AMBIGUOUS"
    assert "host_edit_applied" not in authorization.result.result.model_dump()
    assert authorization.content is None
    preview_call = application._product_realization_preview_service.preview.await_args
    assert preview_call.kwargs["family_version"].version == "exact"
    assert preview_call.kwargs["invocation"].realization_kind == "self_geometry"
    assert "pinned_ifc_revision_digest" not in preview_call.kwargs


@pytest.mark.asyncio
async def test_standard_model_requires_explicit_origin_unit_and_uses_independent_entry() -> None:
    from CAD2BIM_mcp.direct.tool_support import ProviderJsonDocumentError
    missing = _provider_input(attachment_kind="standard_model")
    missing_document = json.loads(missing["invocation_json"])
    missing_document.pop("product_origin")
    missing["invocation_json"] = json.dumps(missing_document)
    with pytest.raises(ProviderJsonDocumentError, match="product_origin"):
        validate_product_realization_preview_provider_input(missing)
    request = validate_product_realization_preview_provider_input(_provider_input(
        attachment_kind="standard_model", product_origin=[1000, 2000, 3000],
        length_unit={"name": "METRE", "prefix": "MILLI"},
    ))
    application = _Application(_outcome(ProductRealizationStatus.SOLVED))
    authorization = await application.preview_ifc_product_realization(request)
    invocation = application._product_realization_preview_service.preview.await_args.kwargs["invocation"]
    assert invocation.realization_kind == "standard_model"
    assert invocation.product_origin == (1000, 2000, 3000)
    assert invocation.length_unit.prefix == "MILLI"
    assert "attachments" not in invocation.model_dump()
    assert "interface_bindings" not in invocation.model_dump()
    assert authorization.result.result.attachment_kind == "standard_model"
    assert "host_edit_applied" not in authorization.result.result.model_dump()












@pytest.mark.asyncio
async def test_tool_solved_content_order_and_inline_png_are_exact() -> None:
    application = _Application(_outcome(ProductRealizationStatus.SOLVED))
    server = FastMCP("product-realization-preview")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)

    async with Client(server) as client:
        response = await client.call_tool(
            "ifc_product_realization_preview",
            _provider_input(),
        )

    assert [item.type for item in response.content] == ["text", "image"]
    assert response.structured_content is None
    assert _preview_json(response)["result"]["status"] == "SOLVED"
    assert _preview_json(response)["result"]["external_target_comparison"] == "NOT_COMPARED"
    assert "product_realization_preview_capability" not in _preview_json(response)["result"]
    assert json.loads(response.content[0].text) == _preview_json(response)
    assert base64.b64decode(response.content[1].data) == _PNG
    serialized = json.dumps(_preview_json(response), sort_keys=True).lower()
    for name in _INTERNAL_NAMES:
        assert f'"{name}"' not in serialized


@pytest.mark.asyncio
async def test_retained_family_render_failure_returns_solved_facts_reference_and_no_image() -> None:
    base = _outcome(ProductRealizationStatus.SOLVED)
    diagnostic = GeometryInspectionDiagnostic(code="PREVIEW_RENDER_FAILED", phase="render",
        message="The retained actual realization could not be rendered.",
        action="Observe the same retained result with a different display.")
    outcome = ProductRealizationPreviewOutcome.model_validate({
        **base.model_dump(), "preview": None, "operation_alias": "op_028",
        "inspection_ref": "cur.family_inspection_exact", "diagnostics": (diagnostic,),
    })
    application = _Application(outcome)
    server = FastMCP("retained-family-render-failure")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)
    async with Client(server) as client:
        response = await client.call_tool("ifc_product_realization_preview", {
            **_provider_input(), "retain": True,
        })
    assert [item.type for item in response.content] == ["text"]
    result = _preview_json(response)["result"]
    assert result["status"] == "SOLVED" and result["render_status"] == "failed"
    assert result["operation_alias"] == "op_028"
    assert result["inspection_ref"] == "cur.family_inspection_exact"
    assert result["instance_receipts"] and result["diagnostics"][0]["phase"] == "render"
    call = application._product_realization_preview_service.preview.await_args
    assert call.kwargs["retain_for_inspection"] is True
    assert call.kwargs["request_identity"]
    with pytest.raises(ValueError):
        ProductRealizationPreviewOutcome.model_validate({**base.model_dump(), "preview": None})
    with pytest.raises(ValueError):
        ProductRealizationPreviewOutcome.model_validate({**base.model_dump(), "preview": None,
            "operation_alias": "op_028", "inspection_ref": "cur.family_inspection_exact"})
    for legacy_omission in (
        "frame_check_status",
        "frame_diagnostics",
        "diagnostic_only",
        "eligible_for_product_write",
        "uniform_scale",
        "host_overlays",
        "straight_profile_overlays",
    ):
        assert legacy_omission not in _preview_json(response)["result"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_requirement", "expected_profile_satisfied"),
    [("none", None), ("equal", True)],
)
async def test_frame_profile_evaluation_matches_provider_output_schema(
    profile_requirement: str,
    expected_profile_satisfied: bool | None,
) -> None:
    relation_document: dict[str, object] = {
        "direction_relation": "opposed",
        "orientation_requirement": "axis_only",
    }
    if profile_requirement == "equal":
        relation_document["profile_requirement"] = "equal"
    relation = InterfaceFrameRelation.model_validate(relation_document)
    profile = CircularInterfaceProfile(diameter_m=0.1)
    evaluation = evaluate_interface_frame_relation(
        RigidTransform3D(),
        RigidTransform3D(rotation=((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))),
        relation,
        position_tolerance_m=1e-6,
        angular_tolerance_rad=1e-6,
        left_profile=profile,
        right_profile=profile,
    )
    assert evaluation.profile_satisfied is expected_profile_satisfied

    diagnostic = ProductRealizationFrameDiagnostic(
        check_kind="interface_coincidence",
        check_key="inlet_outlet",
        interface_key="inlet",
        peer_interface_key="outlet",
        evaluation=evaluation,
    )
    base = _outcome(ProductRealizationStatus.SOLVED)
    realization = base.realization.model_copy(
        update={"frame_check_status": "PASS", "frame_diagnostics": (diagnostic,)}
    )
    application = _Application(
        ProductRealizationPreviewOutcome(
            realization=realization,
            preview=base.preview,
        )
    )
    server = FastMCP(f"product-realization-profile-{profile_requirement}")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)
    request = _provider_input()

    async with Client(server) as client:
        response = await client.call_tool("ifc_product_realization_preview", request)
        native_tool = next(
            tool
            for tool in await client.list_tools()
            if tool.name == "ifc_product_realization_preview"
        )

    projected = _preview_json(response)["result"]["frame_diagnostics"][0]["evaluation"]
    assert native_tool.outputSchema is None
    output_schema = reference_schema_projection(provider_output_schema(
        IfcProductRealizationPreviewResult, compact=True, preserve_references=True,
    ))[0]
    Draft202012Validator(output_schema).validate(_preview_json(response))
    assert projected["satisfied"] is True
    if expected_profile_satisfied is None:
        assert "profile_requirement" not in projected["relation"]
        assert "profile_satisfied" not in projected
    else:
        assert projected["relation"]["profile_requirement"] == "equal"
        assert projected["profile_satisfied"] is True


def test_profile_satisfied_remains_a_backend_only_evaluation_fact() -> None:
    with pytest.raises(ValidationError) as caught:
        InterfaceFrameRelation.model_validate({
            "direction_relation": "same", "orientation_requirement": "axis_only",
            "profile_satisfied": True,
        })
    assert caught.value.errors(include_url=False, include_input=False)[0]["type"] == "extra_forbidden"


@pytest.mark.asyncio
async def test_p4_solve_projects_public_presentation_and_scene_overlays() -> None:
    base = _outcome(ProductRealizationStatus.SOLVED)
    outcome = ProductRealizationPreviewOutcome(
        realization=base.realization,
        preview=_preview_authorization(),
    )
    application = _Application(outcome)
    server = FastMCP("product-realization-modern-preview")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)

    async with Client(server) as client:
        response = await client.call_tool(
            "ifc_product_realization_preview",
            _provider_input(),
        )

    result = _preview_json(response)["result"]
    assert result["status"] == "SOLVED"
    assert result["presentation"]["metadata_kind"] == "rendered"
    assert result["presentation"]["panels"][0]["panel_key"] == "front"
    assert result["presentation"]["panels"][0]["geometry_clipped"] is True
    assert result["presentation"]["panels"][0]["visible_overlay_ids"] == ["family:inlet"]
    assert result["presentation"]["display_quality_warnings"] == [{
        "code": "DISPLAY_MESH_AREA_WARNING",
        "message": outcome.preview.render_metadata.display_quality_warnings[0].message,
    }]
    assert result["presentation"]["connection_context"] == {
        "mode": "pipes", "items": [{"start_m": [0.0, 0.0, 0.0],
            "end_m": [0.0, 0.0, 0.1], "outer_diameter_m": 0.05}],
    }
    assert result["presentation"]["connection_context_diagnostics"] == [
        "Context unavailable for interface 'mount': no outer diameter."]
    assert result["interface_overlays"][0]["marker_kind"] == "family_external"
    for removed in (
        "abi_profile",
        "runtime_profile",
        "budget_profile",
        "input_digest",
        "binding_digest",
        "source_bundle_sha256",
        "solve_digest",
        "host_edit_applied",
        "canonical_geometry_sha256",
        "preview_content_sha256",
    ):
        assert removed not in result
    assert "geometry_sha256" not in result["instance_receipts"][0]


@pytest.mark.asyncio
async def test_product_preview_rejects_stored_presentation_at_output_projection() -> None:
    base = _outcome(ProductRealizationStatus.SOLVED)
    outcome = ProductRealizationPreviewOutcome(
        realization=base.realization,
        preview=base.preview.model_copy(update={
            "render_metadata": GeometryStoredPreviewMetadata(),
        }),
    )
    application = _Application(outcome)
    with pytest.raises(DirectApplicationError) as caught:
        await application.preview_ifc_product_realization(
            validate_product_realization_preview_input(_self_geometry_input())
        )
    assert caught.value.code == "PRODUCT_REALIZATION_PREVIEW_INTEGRITY_ERROR"
    assert caught.value.phase == "output_validation"










@pytest.mark.asyncio
async def test_frame_fail_solved_preview_keeps_diagnostic_geometry_and_typed_facts() -> None:
    base = _outcome(ProductRealizationStatus.SOLVED)
    relation = InterfaceFrameRelation(
        direction_relation="opposed",
        orientation_requirement="full_frame",
        profile_requirement="equal",
    )
    diagnostic = ProductRealizationFrameDiagnostic(
        check_kind="interface_coincidence",
        check_key="inlet_outlet",
        interface_key="inlet",
        peer_interface_key="outlet",
        evaluation=InterfaceFrameEvaluation(
            relation=relation,
            position_tolerance_m=1e-6,
            angular_tolerance_rad=1e-6,
            origin_distance_m=0.002,
            axial_offset_m=0.002,
            lateral_offset_m=0.0,
            direction_residual_rad=0.0,
            orientation_residual_rad=0.01,
            profile_satisfied=True,
            satisfied=False,
        ),
    )
    realization = base.realization.model_copy(
        update={
            "instance_receipts": (
                base.realization.instance_receipts[0].model_copy(update={"uniform_scale": 2.0}),
            ),
            "interface_project_world_frames": (
                ProductRealizationInterfaceWorldFrame(
                    interface_key="inlet",
                    project_world_frame=RigidTransform3D(translation_m=(1.0, 2.0, 3.0)),
                    component_local_frame=RigidTransform3D(translation_m=(0.1, 0.0, 0.0)),
                    profile=RectangleInterfaceProfile(width_m=0.2, height_m=0.1),
                    source=ProductFamilyModelInterfaceSource(
                        fragment_key="body_shape",
                        model_interface_key="port_a",
                    ),
                ),
            ),
            "frame_check_status": "FAIL",
            "frame_diagnostics": (diagnostic,),
        }
    )
    outcome = ProductRealizationPreviewOutcome(
        realization=realization,
        preview=base.preview,
    )
    application = _Application(outcome)
    server = FastMCP("product-realization-frame-fail-preview")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)

    async with Client(server) as client:
        response = await client.call_tool(
            "ifc_product_realization_preview",
            _provider_input(),
        )
    projected = _preview_json(response)["result"]

    assert [item.type for item in response.content] == ["text", "image"]
    assert base64.b64decode(response.content[1].data) == _PNG
    assert projected["frame_check_status"] == "FAIL"
    assert projected["diagnostic_only"] is True
    assert projected["eligible_for_product_write"] is False
    assert projected["frame_diagnostics"][0]["evaluation"]["satisfied"] is False
    assert projected["instance_receipts"][0]["uniform_scale"] == 2.0
    world_fact = projected["interface_project_world_frames"][0]
    assert world_fact["source"] == {
        "source_kind": "model_interface",
        "fragment_key": "body_shape",
        "model_interface_key": "port_a",
    }
    assert world_fact["profile"] == {
        "kind": "rectangle",
        "width_m": 0.2,
        "height_m": 0.1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [ProductRealizationStatus.AMBIGUOUS, ProductRealizationStatus.INFEASIBLE],
)
async def test_tool_non_solved_results_have_text_only(status) -> None:
    application = _Application(_outcome(status))
    server = FastMCP(f"product-realization-preview-{status.value.lower()}")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)

    async with Client(server) as client:
        response = await client.call_tool(
            "ifc_product_realization_preview",
            _provider_input(),
        )

    assert [item.type for item in response.content] == ["text"]
    assert _preview_json(response)["result"]["status"] == status.value
    assert "product_realization_preview_capability" not in _preview_json(response)["result"]





@pytest.mark.asyncio
async def test_runtime_failure_projects_only_the_approved_diagnostic_to_tool_error() -> None:
    application = _Application(_outcome(ProductRealizationStatus.SOLVED))
    application._product_realization_preview_service.preview.side_effect = (
        ProductRealizationPreviewError(
            ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED,
            "runtime_code=GEOMETRY_INSTANTIATION_FAILED; "
            "runtime_phase=geometry_instantiation; "
            "diagnostic=instantiated geometry did not reach a canonical reopen orbit",
        )
    )
    server = FastMCP("product-realization-preview-runtime-error")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)

    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("ifc_product_realization_preview", _provider_input())

    message = str(caught.value)
    assert "RUNTIME_EXECUTION_FAILED" in message
    assert "phase=execution" in message
    assert "retryable=false" in message
    assert "runtime_code=GEOMETRY_INSTANTIATION_FAILED" in message
    assert "runtime_phase=geometry_instantiation" in message
    assert "instantiated geometry did not reach a canonical reopen orbit" in message
    assert "artifact_version_id" not in message
    assert "storage_path" not in message
    assert "/Users/" not in message
    assert len(message.encode("utf-8")) <= 2_000


@pytest.mark.asyncio
async def test_runtime_admission_diagnostic_survives_direct_tool_projection() -> None:
    application = _Application(_outcome(ProductRealizationStatus.SOLVED))
    diagnostic = {"resource_pool": "python", "reason": "queue_full", "phase": "admission", "execution_started": False, "current_stage_started": False}
    application._product_realization_preview_service.preview.side_effect = ProductRealizationPreviewError(
        ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED,
        "Product Realization admission could not complete.", retryable=True, diagnostic=diagnostic,
    )
    server = FastMCP("product-preview-admission-error")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)
    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("ifc_product_realization_preview", _provider_input())
    message = str(caught.value)
    assert "RUNTIME_EXECUTION_FAILED" in message and "retryable=true" in message
    for key, value in diagnostic.items():
        assert json.dumps({key: value}, separators=(",", ":"))[1:-1] in message
    assert len(message.encode()) <= 2_000








@pytest.mark.asyncio
async def test_invalid_focus_request_returns_actionable_input_error() -> None:
    application = _Application(_outcome(ProductRealizationStatus.SOLVED))
    application._product_realization_preview_service.preview.side_effect = (
        ProductRealizationPreviewError(
            ProductRealizationPreviewErrorCode.PREVIEW_REQUEST_INVALID,
            "Increase vertical_extent_m or omit it for automatic framing.",
        )
    )
    server = FastMCP("product-realization-preview-request-error")
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS[0](server, application)
    async with Client(server) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("ifc_product_realization_preview", _provider_input())
    message = str(caught.value)
    assert "PREVIEW_REQUEST_INVALID" in message
    assert "phase=input_validation" in message
    assert "automatic framing" in message
    assert "AUTHORIZATION_FAILED" not in message


@pytest.mark.parametrize("attachment_kind", ["standalone", "explicit_connection_interfaces", "explicit_host_interfaces", "current_ifc"])
def test_retired_internal_preview_branches_are_rejected(attachment_kind):
    with pytest.raises(ValidationError):
        validate_product_realization_preview_input({**_self_geometry_input(), "attachment_kind": attachment_kind})


@pytest.mark.asyncio
async def test_current_input_failure_keeps_safe_invocation_source():
    application = _Application(_outcome(ProductRealizationStatus.SOLVED))
    application._product_realization_preview_service.preview.side_effect = ProductRealizationPreviewError(
        ProductRealizationPreviewErrorCode.INPUT_LOADING_FAILED,
        "input_error_code=INVALID_PUBLIC_PARAMETERS; domain_path=public_parameters.size; correction=Use the exact Family parameter range.",
    )
    with pytest.raises(DirectApplicationError) as caught:
        await application.preview_ifc_product_realization(validate_product_realization_preview_input(_self_geometry_input()))
    assert caught.value.code == "INPUT_LOADING_FAILED"
    assert "provider_input_source=invocation_json" in str(caught.value)
    assert "Use the exact Family parameter range" in str(caught.value)
