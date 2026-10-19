"""FastMCP registration for read-only exact Product Realization preview."""

from __future__ import annotations

from CAD2BIM_backend.domain.preview_cases import FamilyCasesInput, PreviewCaseRender
from CAD2BIM_mcp.direct.ifc.preview_cases import FamilyPreviewSource, CASES_MAX_BYTES
from CAD2BIM_mcp.direct.provider_schema import provider_reference_schema

import base64

from fastmcp import Context, FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import ImageContent, TextContent
from pydantic import ValidationError

from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.ifc.invocation import server_ifc_invocation_key
from CAD2BIM_mcp.direct.geometry_preview_contracts import IfcGeometryPreviewReferences
from CAD2BIM_mcp.direct.ifc.product_family.preview_application import (
    ProductRealizationPreviewProviderApplication,
)
from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import (
    DirectIfcProductRealizationPreviewProviderInput,
    IfcProductRealizationPreviewResult,
    IfcProductRealizationSolvedResult,
    validate_product_realization_preview_provider_input,
    product_realization_invocation_schemas,
)
from CAD2BIM_mcp.tool_docs.ifc_product_family import (
    IFC_PRODUCT_REALIZATION_PREVIEW_DESCRIPTION, PRODUCT_FAMILY_PREVIEW_SCENARIOS_EXAMPLE,
)
from CAD2BIM_mcp.direct.tool_support import (
    DIRECT_SESSION_ALIAS,
    ProviderJsonDocumentError,
    invalid_provider_input,
    provider_validation_message,
    register_provider_tool,
    register_provider_document,
)
from CAD2BIM_mcp.tool_docs import ifc_product_family as _tool_docs


def register_ifc_product_realization_preview(
    server: FastMCP,
    application: ProductRealizationPreviewProviderApplication,
) -> None:
    async def ifc_product_realization_preview(
        direct_session_alias: DIRECT_SESSION_ALIAS,
        ctx: Context,
        mode: str = "single",
        attachment_kind: str | None = None,
        source: FamilyPreviewSource | None = None,
        cases_json: str | None = None,
        render_json: str | None = None,
        nominal_scenario_key: str | None = None,
        invocation_json: str | None = None,
        preview_profile_json: str | None = None,
        reference_geometries: IfcGeometryPreviewReferences | None = None,
        retain: bool = False,
    ) -> ToolResult:
        try:
            value: dict[str, object] = {
                "direct_session_alias": direct_session_alias,
                "mode": mode,
                "retain": retain,
            }
            values = dict(attachment_kind=attachment_kind, source=source, invocation_json=invocation_json,
                          cases_json=cases_json, render_json=render_json, nominal_scenario_key=nominal_scenario_key,
                          preview_profile_json=preview_profile_json, reference_geometries=reference_geometries)
            value.update({key: item for key, item in values.items() if item is not None})
            request = validate_product_realization_preview_provider_input(value)
            authorization = await application.preview_ifc_product_realization(
                request, request_identity=server_ifc_invocation_key(ctx),
            )
            result = authorization.result
            outcome = result.result
            content: list[TextContent | ImageContent] = [
                TextContent(type="text", text=result.model_dump_json(exclude_none=True))
            ]
            if isinstance(outcome, IfcProductRealizationSolvedResult):
                if authorization.content is None or authorization.title is None:
                    raise DirectApplicationError(
                        "PRODUCT_REALIZATION_PREVIEW_INTEGRITY_ERROR",
                        "The solved Product Realization result has no preview image bytes",
                        phase="output_validation",
                    )
                content.append(ImageContent(
                    type="image",
                    data=base64.b64encode(authorization.content).decode("ascii"),
                    mimeType=outcome.preview_mime_type,
                ))
            content.extend(ImageContent(type="image", data=base64.b64encode(item).decode("ascii"),
                                        mimeType="image/png") for item in authorization.images)
            return ToolResult(
                content=content,
                structured_content=result.model_dump(mode="json", exclude_none=True),
            )
        except ValidationError as exc:
            raise invalid_provider_input(
                provider_validation_message(
                    "Use one complete single observation or cases branch",
                    exc,
                ),
                exc,
            ) from exc
        except ProviderJsonDocumentError as exc:
            raise _tool_error(
                DirectApplicationError("INVALID_ARGUMENT", str(exc), phase="preflight")
            ) from None
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    cases_schema = provider_reference_schema(FamilyCasesInput)
    cases_schema["examples"] = [{"mode": "inline", "items": PRODUCT_FAMILY_PREVIEW_SCENARIOS_EXAMPLE[:1]}]
    cases_schema["usage"] = _tool_docs.PRODUCT_REALIZATION_CASES_BUDGET_USAGE
    register_provider_document(server, tool_name="ifc_product_realization_preview", document_name="cases_json",
        description=_tool_docs.PRODUCT_REALIZATION_CASES_RESOURCE_DESCRIPTION,
        document=cases_schema, schema_roots=((),))
    register_provider_document(server, tool_name="ifc_product_realization_preview", document_name="render_json",
        description=_tool_docs.PRODUCT_REALIZATION_RENDER_RESOURCE_DESCRIPTION,
        document=provider_reference_schema(PreviewCaseRender), schema_roots=((),))
    register_provider_document(server, tool_name="ifc_product_realization_preview", document_name="tutorial",
        description=_tool_docs.PRODUCT_REALIZATION_PREVIEW_TUTORIAL_RESOURCE_DESCRIPTION,
        document=_tool_docs.PRODUCT_REALIZATION_PREVIEW_TUTORIAL)
    invocation_schemas = product_realization_invocation_schemas()
    register_provider_document(
        server,
        tool_name="ifc_product_realization_preview",
        document_name="self_geometry",
        description=(
            _tool_docs.PRODUCT_REALIZATION_SELF_GEOMETRY_INVOCATION_RESOURCE_DESCRIPTION
        ),
        document=invocation_schemas["self_geometry"],
        schema_roots=((),),
    )
    register_provider_document(
        server,
        tool_name="ifc_product_realization_preview",
        document_name="standard_model",
        description=(
            _tool_docs.PRODUCT_REALIZATION_STANDARD_MODEL_INVOCATION_RESOURCE_DESCRIPTION
        ),
        document=invocation_schemas["standard_model"],
        schema_roots=((),),
    )
    register_provider_tool(
        server,
        ifc_product_realization_preview,
        name="ifc_product_realization_preview",
        reference_documents={"cases_json": (cases_schema, CASES_MAX_BYTES)},
        compact_input=True,
        input_type=DirectIfcProductRealizationPreviewProviderInput,
        output_type=IfcProductRealizationPreviewResult,
        compact_output=True,
        multimodal_output=True,
        description=IFC_PRODUCT_REALIZATION_PREVIEW_DESCRIPTION,
        annotations={"readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": False},
    )


IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS = (register_ifc_product_realization_preview,)


__all__ = [
    "IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS",
    "register_ifc_product_realization_preview",
]
