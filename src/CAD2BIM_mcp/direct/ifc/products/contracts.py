"""Closed public requests shared by Direct and authenticated Runner adapters."""

from typing import Annotated, Literal, TypeAlias

from pydantic import Field

from CAD2BIM_mcp.direct.contract_base import DirectContract
from CAD2BIM_mcp.direct.ifc.model.contracts import SourceRevision
from CAD2BIM_backend.domain.ifc_products_read import (
    IfcProductsReadAppliedQuery, IfcProductsReadCurrentQuery,
    IfcProductsReadIfcQuery, IfcProductsReadFacts, IfcProductsReadItem,
    IfcProductsReadGroups, IfcReadProductKey, IfcReadGlobalId, MAX_READ_PRODUCTS,
)
from CAD2BIM_mcp.direct.operation.contracts import (
    FailedIfcProductsApplyOperationCard,
    FailedOperationGetResult,
    IfcProductsApplyOperationDetail,
    SucceededIfcProductsApplyOperationCard,
    SucceededOperationGetResult,
    UnsettledIfcProductsApplyResult,
    ProductApplyCommittedRevision,
    SucceededProductsEditCard, FailedProductsEditCard, ProductsEditDetail, UnsettledProductsEditResult,
)
from CAD2BIM_mcp.tool_docs import ifc_products as docs


class ProductsApplyRequest(DirectContract):
    mode: Literal["apply"]
    family_version_alias: str = Field(pattern=r"^cur\..+$", max_length=16_384,
        description=docs.FAMILY_DESCRIPTION)
    package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32,
        description=docs.PACKAGE_DESCRIPTION)
    payload_name: str = Field(min_length=1, max_length=120,
        description=docs.PAYLOAD_DESCRIPTION)


class ProductsRecoverRequest(DirectContract):
    mode: Literal["recover"]
    operation_ref: str = Field(pattern=r"^cur\..+$", max_length=16_384,
        description=docs.RECOVER_DESCRIPTION)


ProductsApplyInput: TypeAlias = Annotated[
    ProductsApplyRequest | ProductsRecoverRequest, Field(discriminator="mode")
]


class DirectProductsApplyRequest(ProductsApplyRequest):
    direct_session_alias: str = Field(pattern=r"^direct_[0-9]{3,}$", max_length=32)


class DirectProductsRecoverRequest(ProductsRecoverRequest):
    direct_session_alias: str = Field(pattern=r"^direct_[0-9]{3,}$", max_length=32)


DirectProductsApplyInput: TypeAlias = Annotated[
    DirectProductsApplyRequest | DirectProductsRecoverRequest, Field(discriminator="mode")
]


class SucceededProductsApplyResult(SucceededOperationGetResult):
    operation: SucceededIfcProductsApplyOperationCard
    detail: IfcProductsApplyOperationDetail


class FailedProductsApplyResult(FailedOperationGetResult):
    operation: FailedIfcProductsApplyOperationCard
    detail: IfcProductsApplyOperationDetail


IfcProductsApplyResult: TypeAlias = (
    SucceededProductsApplyResult | FailedProductsApplyResult | UnsettledIfcProductsApplyResult
)


class ProductsEditApplyRequest(DirectContract):
    mode: Literal["apply"]
    package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32,
        description=docs.EDIT_PACKAGE_DESCRIPTION)
    payload_name: str = Field(min_length=1, max_length=120,
        description=docs.EDIT_PAYLOAD_DESCRIPTION)
    base_revision_ref: str | None = Field(default=None, pattern=r"^cur\..+$", max_length=16_384,
        description=docs.EDIT_BASE_REVISION_DESCRIPTION)


ProductsEditInput: TypeAlias = Annotated[
    ProductsEditApplyRequest | ProductsRecoverRequest, Field(discriminator="mode")]


class DirectProductsEditApplyRequest(ProductsEditApplyRequest):
    direct_session_alias: str = Field(pattern=r"^direct_[0-9]{3,}$", max_length=32)


DirectProductsEditInput: TypeAlias = Annotated[
    DirectProductsEditApplyRequest | DirectProductsRecoverRequest, Field(discriminator="mode")]


class SucceededProductsEditResult(SucceededOperationGetResult):
    operation: SucceededProductsEditCard
    detail: ProductsEditDetail


class FailedProductsEditResult(FailedOperationGetResult):
    operation: FailedProductsEditCard
    detail: ProductsEditDetail


IfcProductsEditResult: TypeAlias = SucceededProductsEditResult | FailedProductsEditResult | UnsettledProductsEditResult


OperationReadReference = Annotated[str, Field(pattern=r"^cur\..+$", max_length=16_384,
    description=docs.READ_OPERATION_REFERENCE_DESCRIPTION)]


class ProductsReadAppliedRequest(IfcProductsReadAppliedQuery):
    mode: Literal["from_apply"]
    operation_ref: OperationReadReference
    product_keys: tuple[IfcReadProductKey, ...] = Field(min_length=1, max_length=MAX_READ_PRODUCTS,
        description=docs.READ_PRODUCT_KEYS_DESCRIPTION)


class ProductsReadCurrentRequest(IfcProductsReadCurrentQuery):
    mode: Literal["from_apply"]
    operation_ref: OperationReadReference
    product_keys: tuple[IfcReadProductKey, ...] = Field(min_length=1, max_length=MAX_READ_PRODUCTS,
        description=docs.READ_PRODUCT_KEYS_DESCRIPTION)
    groups: IfcProductsReadGroups = Field(default_factory=IfcProductsReadGroups,
        description=docs.READ_GROUPS_DESCRIPTION)


class ProductsReadIfcRequest(IfcProductsReadIfcQuery):
    mode: Literal["from_ifc"]
    global_ids: tuple[IfcReadGlobalId, ...] = Field(min_length=1, max_length=MAX_READ_PRODUCTS,
        description=docs.READ_GLOBAL_IDS_DESCRIPTION)
    operation_ref: OperationReadReference | None = None
    groups: IfcProductsReadGroups = Field(default_factory=IfcProductsReadGroups,
        description=docs.READ_GROUPS_DESCRIPTION)


# The applied view may be omitted; complete closed branches preserve that default
# without requiring a missing view discriminator before Pydantic can apply it.
ProductsReadInput: TypeAlias = ProductsReadAppliedRequest | ProductsReadCurrentRequest | ProductsReadIfcRequest


class DirectProductsReadAppliedRequest(ProductsReadAppliedRequest):
    direct_session_alias: str = Field(pattern=r"^direct_[0-9]{3,}$", max_length=32)


class DirectProductsReadCurrentRequest(ProductsReadCurrentRequest):
    direct_session_alias: str = Field(pattern=r"^direct_[0-9]{3,}$", max_length=32)


class DirectProductsReadIfcRequest(ProductsReadIfcRequest):
    direct_session_alias: str = Field(pattern=r"^direct_[0-9]{3,}$", max_length=32)


DirectProductsReadInput: TypeAlias = (
    DirectProductsReadAppliedRequest | DirectProductsReadCurrentRequest | DirectProductsReadIfcRequest
)


class AvailableProductsReadDetails(DirectContract):
    detail_access: Literal["available"] = "available"
    package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)


class UnavailableProductsReadDetails(DirectContract):
    detail_access: Literal["not_in_current_catalog", "unavailable"]


ProductsReadDetails: TypeAlias = AvailableProductsReadDetails | UnavailableProductsReadDetails


class ProductsReadInputNavigation(DirectContract):
    payload_name: str = Field(min_length=1, max_length=120)
    logical_key_fields: tuple[str, ...] = Field(max_length=16)
    details: ProductsReadDetails


class ProductsReadReportNavigation(DirectContract):
    payload_names: tuple[Literal["products", "components", "interfaces", "checks"], ...] = Field(
        default=("products", "components", "interfaces", "checks"), max_length=4)
    details: ProductsReadDetails


class ProductsReadOperation(DirectContract):
    operation_ref: OperationReadReference
    outcome: Literal["prepared", "indeterminate", "succeeded", "failed"]
    family_version_ref: str = Field(min_length=1, max_length=16_384)
    committed_revision: ProductApplyCommittedRevision | None = None
    input: ProductsReadInputNavigation
    report: ProductsReadReportNavigation


class IfcProductsReadResult(IfcProductsReadFacts):
    items: tuple[IfcProductsReadItem, ...] = Field(max_length=MAX_READ_PRODUCTS,
        description=docs.READ_ITEMS_DESCRIPTION)
    operation: ProductsReadOperation | None = None
    source_revision: SourceRevision | None = None
