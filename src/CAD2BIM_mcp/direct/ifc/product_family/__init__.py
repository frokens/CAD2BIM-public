"""Direct Provider surface for ProductFamily discovery, authoring, and preview."""

from CAD2BIM_mcp.direct.ifc.product_family.application import (
    IfcProductFamilyApplicationMixin,
    ProductFamilyProviderApplication,
)
from CAD2BIM_mcp.direct.ifc.product_family.tools import (
    IFC_PRODUCT_FAMILY_TOOL_REGISTRARS as IFC_PRODUCT_FAMILY_READ_TOOL_REGISTRARS,
)
from CAD2BIM_mcp.direct.ifc.product_family.write_application import (
    IfcProductFamilyWriteApplicationMixin,
    ProductFamilyWriteProviderApplication,
)
from CAD2BIM_mcp.direct.ifc.product_family.write_tools import (
    IFC_PRODUCT_FAMILY_WRITE_TOOL_REGISTRARS,
)
from CAD2BIM_mcp.direct.ifc.product_family.preview_application import (
    IfcProductRealizationPreviewApplicationMixin,
    ProductRealizationPreviewProviderApplication,
)
from CAD2BIM_mcp.direct.ifc.product_family.preview_tools import (
    IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS,
)

IFC_PRODUCT_FAMILY_TOOL_REGISTRARS = (
    *IFC_PRODUCT_FAMILY_READ_TOOL_REGISTRARS,
    *IFC_PRODUCT_FAMILY_WRITE_TOOL_REGISTRARS,
    *IFC_PRODUCT_REALIZATION_PREVIEW_TOOL_REGISTRARS,
)

__all__ = [
    "IFC_PRODUCT_FAMILY_TOOL_REGISTRARS",
    "IfcProductFamilyApplicationMixin",
    "IfcProductFamilyWriteApplicationMixin",
    "IfcProductRealizationPreviewApplicationMixin",
    "ProductFamilyProviderApplication",
    "ProductFamilyWriteProviderApplication",
    "ProductRealizationPreviewProviderApplication",
]
