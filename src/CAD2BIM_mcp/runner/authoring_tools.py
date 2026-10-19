"""Runner MCP projection for current Geometry and ProductFamily authoring."""

from __future__ import annotations

import base64
import json
from CAD2BIM_backend.domain.canonical import canonical_json
from typing import Any, Protocol, get_type_hints

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import ImageContent, TextContent
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, create_model

from CAD2BIM_backend.domain.preview_cases import PreviewCaseReport, PreviewCaseBatchOutcome
from CAD2BIM_mcp.direct.ifc.preview_cases import project_case_batch
from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryCatalogListResult, GeometryCatalogRootInfo, GeometryCatalogVersionInfo,
    GeometrySourceReadResult,
)
from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterfaces
from CAD2BIM_backend.domain.geometry_inspection import (
    GeometryInspectionSelection, GeometryInspectionCoordinates, GeometryInspectionDiagnostic,
)
from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionPreviewOutcome
from CAD2BIM_backend.domain.product_realization_preview import FamilyStandardModelPreviewOutcome
from CAD2BIM_backend.domain.geometry_runtime import GeometryPreview, GeometrySectionResult
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceKind, GeometryResourceVersionBundle,
)
from CAD2BIM_backend.domain.product_family_catalog import (
    ProductFamilyCatalogListResult, ProductFamilyCatalogRootInfo,
    ProductFamilyCatalogVersionInfo,
)
from CAD2BIM_backend.domain.product_realization_preview import ProductRealizationPreviewOutcome
from CAD2BIM_mcp.direct.ifc.geometry.application import (
    _geometry_resource_source_result, _geometry_summary, _representative_geometry_sha256,
    _resource_card, _resource_summary, _version_card,
    _project_inspection_preview,
    _project_family_standard_preview,
)
from CAD2BIM_mcp.direct.ifc.geometry.authoring_contracts import IfcGeometryResourceWriteResult
from CAD2BIM_mcp.direct.ifc.geometry.contracts import (
    IfcGeometryBoundingBoxResult,
    IfcGeometryQueryPythonResult, IfcGeometryResourceInfoResult,
    IfcGeometryResourceInputInfo, IfcGeometryResourceListResult,
    IfcGeometryResourceRootInfoResult, IfcGeometryResourceSourceReadResult,
    IfcGeometryResourceVersionInfoResult, IfcGeometryTopologyResult,
    IfcGeometrySourceFileResult,
    IfcGeometryPreviewOutcomeResult,
)
from CAD2BIM_mcp.direct.ifc.product_family.application import (
    _family_card, _family_rejection, _version_card as _family_version_card, _version_info as _family_version_info,
)
from CAD2BIM_mcp.direct.ifc.product_family.contracts import (
    IfcProductFamilyInfoResult, IfcProductFamilyListResult,
    IfcProductFamilyRootInfoResult,
)
from CAD2BIM_mcp.direct.ifc.product_family.preview_application import _project_preview_outcome
from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import IfcProductRealizationPreviewResult
from CAD2BIM_mcp.direct.ifc.product_family.write_contracts import IfcProductFamilyWriteResult
from CAD2BIM_mcp.direct.provider_schema import provider_output_schema, provider_reference_schema

from CAD2BIM_mcp.direct.ifc.authoring_publish import AuthoringPublishResult
from CAD2BIM_mcp.runner.authoring_contracts import (
    AuthoringPublishInput,
    FamilyInfoInput, FamilyListInput, FamilyWriteInput,
    GeometryInfoInput, GeometryListInput, GeometryPreviewInput,
    GeometryQueryInput, GeometrySourceReadInput, GeometryWriteInput,
    ProductRealizationPreviewInput,
    authoring_domain_request,
)
from CAD2BIM_mcp.runner.tool_projection import runner_tool_description, runner_public_document
from CAD2BIM_mcp.direct.tool_support import ProviderFunctionTool, ProviderJsonDocumentError, invalid_provider_input, register_provider_document
from CAD2BIM_mcp.tool_docs import ifc_geometry as geometry_docs, ifc_product_family as family_docs, common as common_docs
from CAD2BIM_mcp.tool_docs.preview import PREVIEW_RESULT_DOCUMENTS
from CAD2BIM_mcp.reference_projection import reference_schema_projection
from fastmcp.exceptions import ToolError
from CAD2BIM_mcp.runner.backend_client import BackendDomainError
from CAD2BIM_mcp.runner.domain_errors import backend_domain_tool_error
from CAD2BIM_mcp.runner.resources import request_domain_credential


_INTERFACES = TypeAdapter(GeometryModelInterfaces)


class AuthoringBackend(Protocol):
    async def call_authoring(
        self, *, access_token: str, tool_name: str, body: dict[str, Any],
    ) -> dict[str, Any]: ...


def _preview_tool_result(result: BaseModel, image: bytes | tuple[bytes, ...] | None) -> ToolResult:
    value = result.model_dump(mode="json", exclude_none=True)
    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    ]
    images = () if image is None else ((image,) if isinstance(image, bytes) else image)
    for image in images:
        content.append(ImageContent(
            type="image", data=base64.b64encode(image).decode("ascii"), mimeType="image/png",
        ))
    return ToolResult(content=content, structured_content=value)


def _geometry_info(payload: dict[str, Any]) -> IfcGeometryResourceInfoResult:
    if payload.get("target_kind") == "draft":
        from CAD2BIM_backend.domain.authoring_draft_content import GeometryDraftInfo
        return IfcGeometryResourceInfoResult(info=GeometryDraftInfo.model_validate(payload))
    catalog_data = payload["catalog"]
    if catalog_data["target_kind"] == "resource_root":
        catalog = GeometryCatalogRootInfo.model_validate(catalog_data)
        bundles = tuple(
            GeometryResourceVersionBundle.model_validate(item)
            for item in payload["version_bundles"]
        )
        if len(bundles) != len(catalog.recent_versions):
            raise ValueError("Geometry root version bundle count changed")
        return IfcGeometryResourceInfoResult(info=IfcGeometryResourceRootInfoResult(
            target_kind="resource_root",
            resource_alias=catalog.resource.resource_root_ref,
            resource_kind=catalog.resource.resource_kind.value,
            owner_scope=catalog.resource.owner_scope.value,
            display_name=catalog.resource.display_name,
            summary=_resource_summary(catalog.resource.resource_kind),
            versions=tuple(_version_card(version, bundle)
                           for version, bundle in zip(catalog.recent_versions, bundles)),
        ))
    catalog = GeometryCatalogVersionInfo.model_validate(catalog_data)
    bundle = GeometryResourceVersionBundle.model_validate(payload["bundle"])
    if bundle.version.content_digest != catalog.version.content_digest:
        raise ValueError("Geometry version Catalog content digest changed")
    geometry_summary = _geometry_summary(bundle)
    _representative_geometry_sha256(bundle)
    manifest = bundle.version.manifest
    local_frame = manifest.get("local_frame")
    if not isinstance(local_frame, dict) or local_frame.get("handedness") != "right":
        raise ValueError("Geometry version local basis is not right handed")
    source = (
        None if payload["source"] is None
        else GeometrySourceReadResult.model_validate(payload["source"])
    )
    source_files = () if source is None else tuple(
        IfcGeometrySourceFileResult(
            source_path=item.source_path, size_bytes=item.size_bytes,
            sha256=item.sha256, text_available=item.text_available,
        ) for item in source.members
    )
    return IfcGeometryResourceInfoResult(info=IfcGeometryResourceVersionInfoResult(
        target_kind="resource_version",
        resource_alias=catalog.resource_root_ref,
        resource_version_alias=catalog.version.resource_version_ref,
        resource_kind=catalog.resource_kind.value,
        owner_scope=catalog.owner_scope.value,
        display_name=catalog.display_name,
        usage_instructions=manifest.get("usage_instructions"),
        version_no=catalog.version.version_no,
        origin_kind=catalog.version.origin_kind,
        geometry_format=geometry_summary.format.value if geometry_summary else None,
        unit="METRE", local_basis="right_handed",
        geometry_inputs=tuple(IfcGeometryResourceInputInfo.model_validate(item)
                              for item in payload["geometry_inputs"]),
        interfaces=_INTERFACES.validate_python(manifest.get("interfaces", ())),
        publication=payload["publication"],
        bbox=IfcGeometryBoundingBoxResult(
            minimum=geometry_summary.bounds.minimum_m,
            maximum=geometry_summary.bounds.maximum_m,
        ) if geometry_summary else None,
        topology=IfcGeometryTopologyResult(
            solid_count=geometry_summary.topology.solid_count,
            shell_count=geometry_summary.topology.shell_count,
            face_count=geometry_summary.topology.face_count,
            edge_count=geometry_summary.topology.edge_count,
            vertex_count=geometry_summary.topology.vertex_count,
            triangle_count=geometry_summary.topology.triangle_count,
        ) if geometry_summary else None,
        source_available=catalog.version.source_available,
        source_files=source_files,
        preview_available=(
            bundle.resource.resource_kind is GeometryResourceKind.MAPPED_MODEL
            or catalog.version.preview_available
        ),
        parameter_schema_json=payload["parameter_schema_json"],
        created_at=catalog.version.created_at,
    ))


def _case_batch(payload):
    outcome = PreviewCaseBatchOutcome(report=PreviewCaseReport.model_validate(payload["case_report"]),
                                      operation_alias=payload["operation_alias"])
    result = project_case_batch(outcome).model_copy(update={"rendered_case_keys": tuple(item["case_key"] for item in payload["images"])})
    return result, tuple(base64.b64decode(item["image_base64"], validate=True) for item in payload["images"])


def _geometry_preview(payload: dict[str, Any]) -> tuple[IfcGeometryPreviewOutcomeResult, bytes | tuple[bytes, ...]]:
    if "case_report" in payload:
        return _case_batch(payload)
    if "inspection_preview" in payload or "family_standard_preview" in payload:
        formal = "family_standard_preview" in payload
        raw = payload["family_standard_preview" if formal else "inspection_preview"]
        def preview(image):
            if image is None:
                return None
            return GeometryPreview.model_validate({
                **image["metadata"], "payload": base64.b64decode(image["image_base64"], validate=True),
            })
        section = raw["section"]
        if section is not None:
            section = GeometrySectionResult.model_validate({
                **section,
                "images": [{**item, "preview": preview(item["preview"])} for item in section["images"]],
            })
        common = {"preview": preview(raw["preview"]), "section": section,
            "diagnostics": TypeAdapter(tuple[GeometryInspectionDiagnostic, ...]).validate_python(raw["diagnostics"])}
        if formal:
            outcome = FamilyStandardModelPreviewOutcome(family_version_ref=raw["family_version_ref"],
                public_parameters_json=raw["public_parameters_json"], **common)
            projected = _project_family_standard_preview(outcome)
        else:
            outcome = GeometryInspectionPreviewOutcome(
            inspection_ref=raw["inspection_ref"], operation_alias=raw["operation_alias"],
            operation_ref=raw["operation_ref"], case_key=raw["case_key"],
            source_kind=raw["source_kind"], source_version_ref=raw["source_version_ref"],
            result_coordinate_space=raw["result_coordinate_space"],
            selection=TypeAdapter(GeometryInspectionSelection).validate_python(raw["selection"]),
            coordinates=TypeAdapter(GeometryInspectionCoordinates).validate_python(raw["coordinates"]),
                **common,
            )
            projected = _project_inspection_preview(outcome)
        images = (() if projected.content is None else (projected.content,)) + projected.images
        return projected.preview, images
    raise ValueError("Geometry Preview response has no current case report or saved observation")


def _family_preview(
    payload: dict[str, Any], request: ProductRealizationPreviewInput,
) -> tuple[IfcProductRealizationPreviewResult, bytes | None]:
    if "case_report" in payload:
        result, images = _case_batch(payload)
        return IfcProductRealizationPreviewResult(result=result), images
    image_data = payload.get("image_base64")
    image = None if image_data is None else base64.b64decode(image_data, validate=True)
    preview = payload.get("preview")
    outcome = ProductRealizationPreviewOutcome.model_validate({
        "realization": payload["realization"],
        "operation_alias": payload.get("operation_alias"),
        "inspection_ref": payload.get("inspection_ref"),
        "diagnostics": payload.get("diagnostics", ()),
        "preview": None if preview is None else {**preview, "content": image},
    })
    projected = _project_preview_outcome(
        request.source.draft_ref if request.source.source_kind == "draft" else request.source.family_version_alias, request.attachment_kind, outcome,
    )
    return projected.result, projected.content


def register_runner_authoring_documents(server: FastMCP) -> None:
    """Reuse public document owners with the Runner request/member projection."""

    from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterface
    from CAD2BIM_backend.domain.product_realization import ProductRealizationAuthorProposal
    from CAD2BIM_backend.domain.preview_cases import GeometryCasesInput, FamilyCasesInput, PreviewCaseRender
    from CAD2BIM_backend.internal_authoring_contracts import GeometryQueryTarget
    from CAD2BIM_mcp.direct.ifc.product_family.write_contracts import product_family_definition_resource_document, product_family_source_changes_schema
    from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import product_realization_invocation_schemas
    from CAD2BIM_mcp.runner.authoring_contracts import FamilySourceChangesDocument
    from CAD2BIM_mcp.direct.tool_support import register_provider_document
    from CAD2BIM_mcp.tool_docs import ifc_geometry as geometry_docs, ifc_product_family as family_docs

    documents = (
        ("ifc_geometry_resource_write", "program_api", geometry_docs.GEOMETRY_AUTHORING_PROGRAM_RESOURCE_DESCRIPTION, {
            "usage": geometry_docs.GEOMETRY_AUTHORING_PROGRAM_USAGE,
            "execution_diagnostics": geometry_docs.GEOMETRY_AUTHORING_EXECUTION_AND_DIAGNOSTIC_GUIDANCE,
            "interface_schema": provider_reference_schema(GeometryModelInterface),
            "return_contract": geometry_docs.GEOMETRY_BUILD_RETURN_CONTRACT,
            "examples": {
                "published_create": {"direct_session_alias": "direct_001", "mode": "create", "source": {"source_kind": "published", "resource_version_alias": "cur.<exact-version>"}},
                "modeling_function_create": json.loads(geometry_docs.GEOMETRY_MODELING_FUNCTION_CREATE_EXAMPLE),
                "draft_update": geometry_docs.GEOMETRY_DRAFT_UPDATE_EXAMPLE,
            },
        }, (("interface_schema",),)),
        ("ifc_geometry_preview", "cases_json", geometry_docs.GEOMETRY_PREVIEW_CASES_RESOURCE_DESCRIPTION,
         provider_reference_schema(GeometryCasesInput), ((),)),
        ("ifc_geometry_preview", "render_json", geometry_docs.GEOMETRY_PREVIEW_RENDER_RESOURCE_DESCRIPTION,
         provider_reference_schema(PreviewCaseRender), ((),)),
        ("ifc_geometry_preview", "tutorial", geometry_docs.GEOMETRY_PREVIEW_TUTORIAL_DESCRIPTION,
         geometry_docs.GEOMETRY_PREVIEW_TUTORIAL, ()),
        ("ifc_geometry_query_python", "program_api", geometry_docs.GEOMETRY_QUERY_PROGRAM_RESOURCE_DESCRIPTION, {
            "usage": geometry_docs.GEOMETRY_QUERY_PROGRAM_USAGE,
            "target_schema": provider_reference_schema(GeometryQueryTarget),
            "return_contract": geometry_docs.GEOMETRY_QUERY_RETURN_CONTRACT,
        }, (("target_schema",),)),
        ("ifc_product_family_write", "definition_json", family_docs.PRODUCT_FAMILY_DEFINITION_RESOURCE_DESCRIPTION,
         product_family_definition_resource_document(),
         (("create_definition_input_schema",), ("update_definition_input_schema",))),
        ("ifc_product_family_write", "source_changes_json", family_docs.PRODUCT_FAMILY_SOURCE_CHANGES_RESOURCE_DESCRIPTION,
         product_family_source_changes_schema(FamilySourceChangesDocument), ((),)),
        ("ifc_product_family_write", "program_api", family_docs.PRODUCT_FAMILY_PROGRAM_RESOURCE_DESCRIPTION, {
            "usage": family_docs.PRODUCT_FAMILY_PROGRAM_API,
            "return_schema": provider_reference_schema(ProductRealizationAuthorProposal),
        }, (("return_schema",),)),
        ("ifc_product_family_write", "tutorial", family_docs.PRODUCT_FAMILY_WRITE_TUTORIAL_RESOURCE_DESCRIPTION,
         family_docs.PRODUCT_FAMILY_WRITE_TUTORIAL, ()),
        ("ifc_product_realization_preview", "cases_json", family_docs.PRODUCT_REALIZATION_CASES_RESOURCE_DESCRIPTION,
         provider_reference_schema(FamilyCasesInput), ((),)),
        ("ifc_product_realization_preview", "render_json", family_docs.PRODUCT_REALIZATION_RENDER_RESOURCE_DESCRIPTION,
         provider_reference_schema(PreviewCaseRender), ((),)),
        ("ifc_product_realization_preview", "tutorial", family_docs.PRODUCT_REALIZATION_PREVIEW_TUTORIAL_RESOURCE_DESCRIPTION,
         family_docs.PRODUCT_REALIZATION_PREVIEW_TUTORIAL, ()),
        ("ifc_product_realization_preview", "self_geometry", family_docs.PRODUCT_REALIZATION_SELF_GEOMETRY_INVOCATION_RESOURCE_DESCRIPTION,
         product_realization_invocation_schemas()["self_geometry"], ((),)),
        ("ifc_product_realization_preview", "standard_model", family_docs.PRODUCT_REALIZATION_STANDARD_MODEL_INVOCATION_RESOURCE_DESCRIPTION,
         product_realization_invocation_schemas()["standard_model"], ((),)),
    )
    for tool_name, document_name, description, document, schema_roots in documents:
        register_provider_document(server, tool_name=tool_name, document_name=document_name,
                                   description=description, document=runner_public_document(tool_name, document), schema_roots=schema_roots)


def register_runner_authoring_tools(server: FastMCP, backend: AuthoringBackend) -> None:
    register_runner_authoring_documents(server)
    async def call(name: str, request: Any) -> dict[str, Any]:
        try:
            domain = authoring_domain_request(name, request)
            return await backend.call_authoring(
                access_token=request_domain_credential(),
                tool_name=name,
                body=domain.model_dump(mode="json", by_alias=True, exclude_unset=True),
            )
        except ValidationError as exc:
            raise invalid_provider_input("Use one complete current public request and its strict JSON documents", exc) from None
        except ProviderJsonDocumentError as exc:
            raise ToolError(f"INVALID_ARGUMENT; phase=preflight; retryable=false; operation_status=not_started; message={exc}") from None
        except BackendDomainError as exc:
            raise backend_domain_tool_error(exc) from None

    async def ifc_geometry_resource_list(request: GeometryListInput) -> IfcGeometryResourceListResult:
        raw = GeometryCatalogListResult.model_validate(
            await call("ifc_geometry_resource_list", request)
        )
        return IfcGeometryResourceListResult(
            resources=tuple(_resource_card(item) for item in raw.resources),
            complete=raw.next_cursor is None, next_cursor=raw.next_cursor,
        )

    async def ifc_geometry_resource_info(request: GeometryInfoInput) -> IfcGeometryResourceInfoResult:
        return _geometry_info(await call("ifc_geometry_resource_info", request))

    async def ifc_geometry_resource_source_read(
        request: GeometrySourceReadInput,
    ) -> IfcGeometryResourceSourceReadResult:
        raw = GeometrySourceReadResult.model_validate(
            await call("ifc_geometry_resource_source_read", request)
        )
        return _geometry_resource_source_result(raw)

    async def ifc_authoring_publish(request: AuthoringPublishInput) -> AuthoringPublishResult:
        return AuthoringPublishResult.model_validate(await call("ifc_authoring_publish", request))

    async def ifc_geometry_resource_write(request: GeometryWriteInput) -> IfcGeometryResourceWriteResult:
        return IfcGeometryResourceWriteResult.model_validate(
            await call("ifc_geometry_resource_write", request)
        )

    async def ifc_geometry_preview(request: GeometryPreviewInput) -> ToolResult:
        result, image = _geometry_preview(await call("ifc_geometry_preview", request))
        return _preview_tool_result(result, image)

    async def ifc_geometry_query_python(request: GeometryQueryInput) -> IfcGeometryQueryPythonResult:
        return IfcGeometryQueryPythonResult.model_validate(
            await call("ifc_geometry_query_python", request)
        )

    async def ifc_product_family_list(request: FamilyListInput) -> IfcProductFamilyListResult:
        raw = ProductFamilyCatalogListResult.model_validate(
            await call("ifc_product_family_list", request)
        )
        return IfcProductFamilyListResult(
            families=tuple(_family_card(item) for item in raw.families),
            rejections=tuple(_family_rejection(item) for item in raw.rejections),
            complete=raw.next_cursor is None, next_cursor=raw.next_cursor,
        )

    async def ifc_product_family_info(request: FamilyInfoInput) -> IfcProductFamilyInfoResult:
        raw = await call("ifc_product_family_info", request)
        if raw["target_kind"] == "draft":
            from CAD2BIM_backend.domain.authoring_draft_content import FamilyDraftInfo
            return IfcProductFamilyInfoResult(info=FamilyDraftInfo.model_validate(raw))
        if raw["target_kind"] == "family_root":
            catalog = ProductFamilyCatalogRootInfo.model_validate(raw)
            return IfcProductFamilyInfoResult(info=IfcProductFamilyRootInfoResult(
                target_kind="family_root", family=_family_card(catalog.family),
                versions=tuple(_family_version_card(item) for item in catalog.versions),
                versions_complete=catalog.versions_complete,
                next_before_version_no=catalog.next_before_version_no,
            ))
        publication = raw.pop("publication")
        catalog = ProductFamilyCatalogVersionInfo.model_validate(raw)
        standard = catalog.family_contract.standard_model
        return IfcProductFamilyInfoResult(info=_family_version_info(catalog).model_copy(update={
            "publication": publication, "standard_model_available": standard is not None,
            "standard_public_parameters_json": canonical_json(standard.public_parameters) if standard is not None else None}))

    async def ifc_product_family_write(request: FamilyWriteInput) -> IfcProductFamilyWriteResult:
        return IfcProductFamilyWriteResult.model_validate(
            await call("ifc_product_family_write", request)
        )

    async def ifc_product_realization_preview(request: ProductRealizationPreviewInput) -> ToolResult:
        result, image = _family_preview(
            await call("ifc_product_realization_preview", request), request,
        )
        return _preview_tool_result(result, image)

    from CAD2BIM_backend.domain.preview_cases import GeometryCasesInput, FamilyCasesInput
    from CAD2BIM_mcp.direct.ifc.product_family.write_contracts import product_family_definition_reference_document
    definition, maximum = product_family_definition_reference_document()
    create_definition, create_maximum = product_family_definition_reference_document(create=True)
    documents = {
        "ifc_product_family_write": {
            "request.definition_json": (definition, maximum),
            "request.source.definition_json": (create_definition, create_maximum),
        },
        "ifc_geometry_preview": {"request.cases_json": (provider_reference_schema(GeometryCasesInput), 1_048_576)},
        "ifc_product_realization_preview": {"request.cases_json": (provider_reference_schema(FamilyCasesInput), 1_048_576)},
    }
    for function, description, output_type, effect in (
        (ifc_geometry_resource_list, geometry_docs.IFC_GEOMETRY_RESOURCE_LIST_DESCRIPTION, IfcGeometryResourceListResult, "READ"),
        (ifc_geometry_resource_info, geometry_docs.IFC_GEOMETRY_RESOURCE_INFO_DESCRIPTION, IfcGeometryResourceInfoResult, "READ"),
        (ifc_geometry_resource_source_read, geometry_docs.IFC_GEOMETRY_RESOURCE_SOURCE_READ_DESCRIPTION, IfcGeometryResourceSourceReadResult, "READ"),
        (ifc_geometry_resource_write, geometry_docs.IFC_GEOMETRY_RESOURCE_WRITE_DESCRIPTION, IfcGeometryResourceWriteResult, "WRITE"),
        (ifc_geometry_preview, geometry_docs.IFC_GEOMETRY_PREVIEW_DESCRIPTION, IfcGeometryPreviewOutcomeResult, "WRITE"),
        (ifc_geometry_query_python, geometry_docs.IFC_GEOMETRY_QUERY_PYTHON_DESCRIPTION, IfcGeometryQueryPythonResult, "READ"),
        (ifc_product_family_list, family_docs.IFC_PRODUCT_FAMILY_LIST_DESCRIPTION, IfcProductFamilyListResult, "READ"),
        (ifc_product_family_info, family_docs.IFC_PRODUCT_FAMILY_INFO_DESCRIPTION, IfcProductFamilyInfoResult, "READ"),
        (ifc_product_family_write, family_docs.IFC_PRODUCT_FAMILY_WRITE_DESCRIPTION, IfcProductFamilyWriteResult, "WRITE"),
        (ifc_product_realization_preview, family_docs.IFC_PRODUCT_REALIZATION_PREVIEW_DESCRIPTION, IfcProductRealizationPreviewResult, "WRITE"),
        (ifc_authoring_publish, common_docs.AUTHORING_PUBLISH_DESCRIPTION, AuthoringPublishResult, "WRITE"),
    ):
        name = function.__name__
        multimodal = name in PREVIEW_RESULT_DOCUMENTS
        output_schema, output_paths = reference_schema_projection(provider_output_schema(output_type, compact=True, preserve_references=multimodal))
        description = runner_tool_description(name, description)
        if multimodal:
            register_provider_document(server, tool_name=name, document_name="result",
                description=common_docs.MULTIMODAL_RESULT_GUIDE_DESCRIPTION, document=PREVIEW_RESULT_DOCUMENTS[name])
            description += common_docs.MULTIMODAL_RESULT_USAGE_DESCRIPTION.format(tool_name=name)
        tool = ProviderFunctionTool.from_function(function, name=name,
            description=description, meta={"effect": effect, "parallel_safe": effect == "READ"},
            output_schema=None if multimodal else output_schema,
            annotations={"readOnlyHint": effect == "READ", "destructiveHint": False,
                         "idempotentHint": multimodal or effect == "READ", "openWorldHint": False})
        tool._reference_documents = documents.get(name, {})
        tool._reference_output_paths = output_paths
        tool._provider_input_adapter = TypeAdapter(create_model(
            name + "Request", request=(get_type_hints(function)["request"], ...),
            __config__=ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False),
        ))
        server.add_tool(tool)


__all__ = ["AuthoringBackend", "register_runner_authoring_tools"]
