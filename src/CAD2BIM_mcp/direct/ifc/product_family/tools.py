"""FastMCP registrations for ready ProductFamily Catalog discovery and inspection."""

from __future__ import annotations

from fastmcp import FastMCP
from pydantic import ValidationError

from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.ifc.product_family.application import (
    ProductFamilyProviderApplication,
)
from CAD2BIM_mcp.direct.ifc.product_family.contracts import (
    DirectIfcProductFamilyInfoInput,
    DirectIfcProductFamilyListInput,
    IfcProductFamilyInfoResult,
    IfcProductFamilyInfoTarget,
    IfcProductFamilyListResult,
    IfcProductFamilyOwnerScope,
    IfcProductStructureKind,
    validate_product_family_list_input,
)
from CAD2BIM_mcp.direct.tool_support import (
    DIRECT_SESSION_ALIAS as _DIRECT_ALIAS,
    invalid_provider_input as _invalid,
    register_provider_tool as _register,
)
from CAD2BIM_mcp.tool_docs import ifc_product_family as _tool_docs


def register_ifc_product_family_list(
    server: FastMCP,
    application: ProductFamilyProviderApplication,
) -> None:
    async def ifc_product_family_list(
        direct_session_alias: _DIRECT_ALIAS,
        owner_scopes: tuple[IfcProductFamilyOwnerScope, ...] = (),
        product_structure_kinds: tuple[IfcProductStructureKind, ...] = (),
        purpose_contains: str | None = None,
        page_size: int = 50,
        cursor: str | None = None,
    ) -> IfcProductFamilyListResult:
        try:
            value: dict[str, object] = {"direct_session_alias": direct_session_alias}
            if cursor is None:
                value.update(
                    owner_scopes=owner_scopes,
                    product_structure_kinds=product_structure_kinds,
                    purpose_contains=purpose_contains,
                    page_size=page_size,
                )
            else:
                value["cursor"] = cursor
            request = validate_product_family_list_input(value)
            return await application.list_ifc_product_families(request)
        except ValidationError as exc:
            raise _invalid(
                "Submit either complete first-page family filters or one unchanged cursor",
                exc,
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    _register(
        server,
        ifc_product_family_list,
        name="ifc_product_family_list",
        compact_input=True,
        input_type=DirectIfcProductFamilyListInput,
        output_type=IfcProductFamilyListResult,
        description=(_tool_docs.IFC_PRODUCT_FAMILY_LIST_DESCRIPTION),
    )


def register_ifc_product_family_info(
    server: FastMCP,
    application: ProductFamilyProviderApplication,
) -> None:
    async def ifc_product_family_info(
        direct_session_alias: _DIRECT_ALIAS,
        target: IfcProductFamilyInfoTarget,
    ) -> IfcProductFamilyInfoResult:
        try:
            request = DirectIfcProductFamilyInfoInput(
                direct_session_alias=direct_session_alias,
                target=target,
            )
            return await application.get_ifc_product_family_info(request)
        except ValidationError as exc:
            raise _invalid("Use one complete family_root or family_version target", exc) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    _register(
        server,
        ifc_product_family_info,
        name="ifc_product_family_info",
        compact_input=True,
        input_type=DirectIfcProductFamilyInfoInput,
        output_type=IfcProductFamilyInfoResult,
        compact_output=True,
        description=(_tool_docs.IFC_PRODUCT_FAMILY_INFO_DESCRIPTION),
    )


IFC_PRODUCT_FAMILY_TOOL_REGISTRARS = (
    register_ifc_product_family_list,
    register_ifc_product_family_info,
)


__all__ = [
    "IFC_PRODUCT_FAMILY_TOOL_REGISTRARS",
    "register_ifc_product_family_info",
    "register_ifc_product_family_list",
]
