from __future__ import annotations

from unittest.mock import Mock

from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.ifc.product_family import IFC_PRODUCT_FAMILY_TOOL_REGISTRARS
from CAD2BIM_mcp.direct.server import create_direct_mcp


_PRODUCT_FAMILY_TOOL_ORDER = (
    "ifc_product_family_list",
    "ifc_product_family_info",
    "ifc_product_family_write",
    "ifc_product_realization_preview",
)


async def test_product_family_registrars_have_one_authoritative_public_order() -> None:
    assert tuple(registrar.__name__ for registrar in IFC_PRODUCT_FAMILY_TOOL_REGISTRARS) == (
        "register_ifc_product_family_list",
        "register_ifc_product_family_info",
        "register_ifc_product_family_write",
        "register_ifc_product_realization_preview",
    )

    server = create_direct_mcp(Mock())
    tool_names = tuple(tool.name for tool in await server.list_tools())
    product_family_tools = tuple(name for name in tool_names if name in _PRODUCT_FAMILY_TOOL_ORDER)

    assert product_family_tools == _PRODUCT_FAMILY_TOOL_ORDER
    assert all(tool_names.count(name) == 1 for name in _PRODUCT_FAMILY_TOOL_ORDER)


def test_direct_facade_retains_one_product_family_service_set() -> None:
    catalog = object()
    authoring = object()
    preview = object()

    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        Mock(),
        product_family_catalog_service=catalog,
        product_family_authoring_service=authoring,
        product_realization_preview_service=preview,
    )

    assert facade._product_family_catalog_service is catalog
    assert facade._product_family_authoring_service is authoring
    assert facade._product_realization_preview_service is preview
    assert callable(facade.write_ifc_product_family)
    assert callable(facade.preview_ifc_product_realization)
