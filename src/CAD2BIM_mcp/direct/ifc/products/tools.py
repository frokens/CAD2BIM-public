"""Register strict Apply and original-action recovery with one closed Provider contract."""

from fastmcp import Context, FastMCP
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.ifc.invocation import server_ifc_invocation_key
from CAD2BIM_mcp.direct.ifc.products.contracts import (
    DirectProductsApplyInput, IfcProductsApplyResult, DirectProductsReadInput, IfcProductsReadResult,
    DirectProductsEditInput, IfcProductsEditResult,
)
from CAD2BIM_backend.domain.ifc_products_read import IfcProductsReadGroups
from CAD2BIM_mcp.direct.tool_support import (
    DIRECT_SESSION_ALIAS, invalid_provider_input, register_provider_document, register_provider_tool,
)
from CAD2BIM_mcp.tool_docs import ifc_products as docs


def register_ifc_products_apply(server: FastMCP, application) -> None:
    async def ifc_products_apply(direct_session_alias: DIRECT_SESSION_ALIAS, mode: str, ctx: Context,
        family_version_alias: str | None = None, package_alias: str | None = None,
        payload_name: str | None = None, operation_ref: str | None = None) -> IfcProductsApplyResult:
        try:
            values = {"direct_session_alias": direct_session_alias, "mode": mode}
            values.update({key: value for key, value in dict(family_version_alias=family_version_alias,
                package_alias=package_alias, payload_name=payload_name, operation_ref=operation_ref).items()
                if value is not None})
            request = TypeAdapter(DirectProductsApplyInput).validate_python(values)
            return await application.apply_ifc_products(request, request_identity=server_ifc_invocation_key(ctx))
        except ValidationError as exc:
            raise invalid_provider_input("Use one complete apply or recover branch; do not mix fields", exc) from None
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    register_provider_document(server, tool_name="ifc_products_apply", document_name="input",
        description=docs.INPUT_GUIDE_DESCRIPTION,
        document=docs.INPUT_GUIDE)
    register_provider_tool(server, ifc_products_apply, name="ifc_products_apply", description=docs.APPLY_DESCRIPTION,
        input_type=DirectProductsApplyInput, output_type=IfcProductsApplyResult,
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
        compact_input=True, compact_output=True)


def register_ifc_products_edit(server: FastMCP, application) -> None:
    async def ifc_products_edit(direct_session_alias: DIRECT_SESSION_ALIAS, mode: str, ctx: Context,
        package_alias: str | None = None, payload_name: str | None = None,
        base_revision_ref: str | None = None, operation_ref: str | None = None) -> IfcProductsEditResult:
        try:
            values = {"direct_session_alias": direct_session_alias, "mode": mode}
            values.update({key: value for key, value in dict(package_alias=package_alias,
                payload_name=payload_name, base_revision_ref=base_revision_ref, operation_ref=operation_ref).items()
                if value is not None})
            request = TypeAdapter(DirectProductsEditInput).validate_python(values)
            return await application.edit_ifc_products(request, request_identity=server_ifc_invocation_key(ctx))
        except ValidationError as exc:
            raise invalid_provider_input("Use one complete edit apply or recover branch; recovery accepts only the original operation_ref", exc) from None
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    register_provider_document(server, tool_name="ifc_products_edit", document_name="input",
        description=docs.EDIT_INPUT_GUIDE_DESCRIPTION, document=docs.EDIT_INPUT_GUIDE)
    register_provider_tool(server, ifc_products_edit, name="ifc_products_edit", description=docs.EDIT_DESCRIPTION,
        input_type=DirectProductsEditInput, output_type=IfcProductsEditResult,
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
        compact_input=True, compact_output=True)


def register_ifc_products_read(server: FastMCP, application) -> None:
    async def ifc_products_read(direct_session_alias: DIRECT_SESSION_ALIAS, mode: str,
        operation_ref: str | None = None, product_keys: list[str] | None = None,
        view: str | None = None, global_ids: list[str] | None = None,
        groups: IfcProductsReadGroups | None = None) -> IfcProductsReadResult:
        try:
            values = {"direct_session_alias": direct_session_alias, "mode": mode}
            values.update({name: value for name, value in dict(operation_ref=operation_ref,
                product_keys=product_keys, view=view, global_ids=global_ids, groups=groups).items()
                if value is not None})
            request = TypeAdapter(DirectProductsReadInput).validate_python(values)
            return await application.read_ifc_products(request)
        except ValidationError as exc:
            raise invalid_provider_input("Use one complete from_apply applied/current or from_ifc branch; applied accepts no current groups", exc) from None
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    register_provider_document(server, tool_name="ifc_products_read", document_name="usage",
        description=docs.READ_GUIDE_DESCRIPTION,
        document=docs.READ_GUIDE)
    register_provider_tool(server, ifc_products_read, name="ifc_products_read", description=docs.READ_DESCRIPTION,
        input_type=DirectProductsReadInput, output_type=IfcProductsReadResult,
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        compact_input=True)
