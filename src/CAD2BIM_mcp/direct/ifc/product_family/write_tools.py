"""FastMCP registration for the single TASK ProductFamily authoring write."""

from __future__ import annotations

from fastmcp import Context, FastMCP
from pydantic import ValidationError

from CAD2BIM_backend.domain.product_realization import ProductRealizationAuthorProposal
from CAD2BIM_backend.domain.text_source import TextChange
from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.ifc.invocation import server_ifc_invocation_key
from CAD2BIM_mcp.direct.ifc.product_family.write_application import (
    ProductFamilyWriteProviderApplication,
)
from CAD2BIM_mcp.direct.ifc.product_family.write_contracts import (
    DirectIfcProductFamilyWriteProviderInput,
    IfcProductFamilyDraftCreateSource,
    IfcProductFamilyWriteResult,
    validate_product_family_write_provider_input,
    product_family_definition_reference_document,
    product_family_definition_resource_document,
    product_family_source_changes_schema,
)
from CAD2BIM_mcp.direct.tool_support import (
    DIRECT_SESSION_ALIAS,
    ProviderJsonDocumentError,
    invalid_provider_input,
    provider_validation_message,
    register_provider_tool,
    register_provider_document,
)
from CAD2BIM_mcp.direct.references import reference_schema_projection
from CAD2BIM_mcp.direct.provider_schema import provider_reference_schema
from CAD2BIM_mcp.tool_docs.ifc_product_family import (
    IFC_PRODUCT_FAMILY_WRITE_DESCRIPTION, PRODUCT_FAMILY_PROGRAM_API,
    PRODUCT_FAMILY_DEFINITION_RESOURCE_DESCRIPTION,
    PRODUCT_FAMILY_SOURCE_CHANGES_RESOURCE_DESCRIPTION,
    PRODUCT_FAMILY_PROGRAM_RESOURCE_DESCRIPTION,
    PRODUCT_FAMILY_WRITE_TUTORIAL,
    PRODUCT_FAMILY_WRITE_TUTORIAL_RESOURCE_DESCRIPTION,
)


_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
}
def register_ifc_product_family_write(
    server: FastMCP,
    application: ProductFamilyWriteProviderApplication,
) -> None:
    async def ifc_product_family_write(
        direct_session_alias: DIRECT_SESSION_ALIAS,
        ctx: Context,
        mode: str,
        display_name: str | None = None,
        purpose: str | None = None,
        usage_instructions: TextChange | None = None,
        definition_json: str | None = None,
        source: IfcProductFamilyDraftCreateSource | None = None,
        draft_ref: str | None = None,
        expected_edit_token: int | None = None,
        source_changes_json: str | None = None,
    ) -> IfcProductFamilyWriteResult:
        try:
            value: dict[str, object] = {
                "direct_session_alias": direct_session_alias,
                "mode": mode,
            }
            optional_values: dict[str, object | None] = {
                "display_name": display_name,
                "purpose": purpose,
                "usage_instructions": usage_instructions,
                "definition_json": definition_json,
                "source": source,
                "draft_ref": draft_ref,
                "expected_edit_token": expected_edit_token,
                "source_changes_json": source_changes_json,
            }
            value.update({key: item for key, item in optional_values.items() if item is not None})
            request = validate_product_family_write_provider_input(value)
            return await application.write_ifc_product_family(
                request,
                request_identity=server_ifc_invocation_key(ctx),
            )
        except ValidationError as exc:
            raise invalid_provider_input(
                provider_validation_message(
                    "Use one complete create or update draft branch",
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

    definition, maximum = product_family_definition_reference_document()
    create_definition, create_maximum = product_family_definition_reference_document(create=True)
    definition_document = product_family_definition_resource_document()
    for root in ("create_definition_input_schema", "update_definition_input_schema"):
        definition_document[root] = reference_schema_projection(definition_document[root])[0]
    register_provider_document(
        server, tool_name="ifc_product_family_write", document_name="definition_json",
        description=PRODUCT_FAMILY_DEFINITION_RESOURCE_DESCRIPTION,
        document=definition_document,
        schema_roots=(("create_definition_input_schema",), ("update_definition_input_schema",)),
    )
    register_provider_document(
        server, tool_name="ifc_product_family_write", document_name="source_changes_json",
        description=PRODUCT_FAMILY_SOURCE_CHANGES_RESOURCE_DESCRIPTION,
        document=product_family_source_changes_schema(),
        schema_roots=((),),
    )
    register_provider_document(
        server, tool_name="ifc_product_family_write", document_name="program_api",
        description=PRODUCT_FAMILY_PROGRAM_RESOURCE_DESCRIPTION,
        document={"usage": PRODUCT_FAMILY_PROGRAM_API,
                  "return_schema": provider_reference_schema(ProductRealizationAuthorProposal)},
        schema_roots=(("return_schema",),),
    )
    register_provider_document(
        server,
        tool_name="ifc_product_family_write",
        document_name="tutorial",
        description=PRODUCT_FAMILY_WRITE_TUTORIAL_RESOURCE_DESCRIPTION,
        document=PRODUCT_FAMILY_WRITE_TUTORIAL,
    )
    register_provider_tool(
        server,
        ifc_product_family_write,
        name="ifc_product_family_write",
        compact_input=True,
        input_type=DirectIfcProductFamilyWriteProviderInput,
        output_type=IfcProductFamilyWriteResult,
        annotations=_WRITE_ANNOTATIONS,
        description=IFC_PRODUCT_FAMILY_WRITE_DESCRIPTION,
        reference_documents={"definition_json": (definition, maximum), "source.definition_json": (create_definition, create_maximum)},
    )


IFC_PRODUCT_FAMILY_WRITE_TOOL_REGISTRARS = (register_ifc_product_family_write,)


__all__ = [
    "IFC_PRODUCT_FAMILY_WRITE_TOOL_REGISTRARS",
    "register_ifc_product_family_write",
]
