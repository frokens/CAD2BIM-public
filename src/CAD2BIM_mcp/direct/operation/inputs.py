"""Closed Operation discovery branches without changing Catalog pagination."""

from typing import Literal, TypeAlias

from pydantic import Field, StrictInt

from CAD2BIM_mcp.direct.contract_base import DirectContract
from CAD2BIM_mcp.direct.transform.common_inputs import DirectOperationListInput
from CAD2BIM_mcp.tool_docs import operation as _tool_docs


class IfcProductsRecoveryListInput(DirectContract):
    mode: Literal["ifc_products_recovery"]
    tool_name: Literal["ifc_products_apply", "ifc_products_edit"]
    cursor: str | None = Field(default=None, pattern=r"^cur\..+$", max_length=16_384,
        description=_tool_docs.IFC_APPLY_RECOVERY_LIST_INPUT_CURSOR_DESCRIPTION)
    page_size: StrictInt = Field(default=20, ge=1, le=50)


class DirectIfcProductsRecoveryListInput(IfcProductsRecoveryListInput):
    direct_session_alias: str = Field(pattern=r"^direct_[0-9]{3,}$", max_length=32)


class DirectOperationCatalogListInput(DirectOperationListInput):
    mode: Literal["catalog"] = "catalog"


DirectOperationListRequest: TypeAlias = DirectOperationCatalogListInput | DirectIfcProductsRecoveryListInput
