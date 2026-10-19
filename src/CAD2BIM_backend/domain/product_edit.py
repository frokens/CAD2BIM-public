"""Typed contracts for atomic edits of existing physical IFC products."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.ifc_product_compilation import IfcProductComponentBinding
from CAD2BIM_backend.domain.product_family import ProductFamilyContract
from CAD2BIM_backend.domain.product_realization import ProductRealizationInvocation


class ProductEditContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class EditIfcProductsCommand(ProductEditContract):
    package_id: str = Field(min_length=1, max_length=80)
    payload_name: str = Field(min_length=1, max_length=120)
    base_revision_id: str | None = Field(default=None, min_length=1, max_length=80)


class _EditRow(ProductEditContract):
    input_row_index: int = Field(ge=0)
    product_key: str = Field(min_length=1)
    target_global_id: str = Field(pattern=r"^[0-9A-Za-z_$]{22}$")


class ReplaceProductEditRow(_EditRow):
    action: Literal["replace"] = "replace"
    family_version_id: str = Field(min_length=1, max_length=80)
    invocation: ProductRealizationInvocation


class PlacementProductEditRow(_EditRow):
    action: Literal["placement"] = "placement"
    placement: RigidTransform3D


class DeleteProductEditRow(_EditRow):
    action: Literal["delete"] = "delete"


ProductEditActionRow = Annotated[
    ReplaceProductEditRow | PlacementProductEditRow | DeleteProductEditRow,
    Field(discriminator="action"),
]


class ProductEditCheck(ProductEditContract):
    check_index: int = Field(ge=0)
    input_row_index: int | None = Field(default=None, ge=0)
    product_key: str | None = None
    target_global_id: str | None = None
    related_global_id: str | None = None
    component_key: str | None = None
    fragment_key: str | None = None
    interface_key: str | None = None
    peer_interface_key: str | None = None
    phase: str
    check_kind: str
    status: Literal["PASS", "FAIL", "WARN", "NOT_RUN", "UNKNOWN"]
    metric: str | None = None
    residual: float | None = None
    tolerance: float | None = None
    unit: str | None = None
    field_path: str | None = None
    actual_json: str | None = None
    expected_json: str | None = None
    error_code: str | None = None
    message: str | None = None
    error_origin: str | None = None
    recommended_action: str | None = None
    resource_version_id: str | None = None
    diagnostic_json: str | None = None


class ProductEditChange(ProductEditContract):
    change_index: int = Field(ge=0)
    input_row_index: int = Field(ge=0)
    change_kind: Literal["created", "deleted", "geometry_replaced", "placement_changed", "relation_detached", "relation_rebound"]
    global_id: str
    ifc_class: str
    component_key: str | None = None
    parent_component_key: str | None = None
    related_global_id: str | None = None
    relation_class: str | None = None
    relation_global_id: str | None = None
    relation_role: str | None = None
    relation_name: str | None = None
    relation_removed: bool | None = None
    before_placement_json: str | None = None
    after_placement_json: str | None = None


class IfcProductsEditTarget(_EditRow):
    action: Literal["replace", "placement", "delete"]
    replacement_ifc_class: str | None = None


class IfcProductEditOwnedProduct(ProductEditContract):
    global_id: str
    ifc_class: str
    parent_global_id: str | None = None
    ownership_relation_class: str | None = None


class IfcProductEditPreflightProduct(ProductEditContract):
    input_row_index: int = Field(ge=0)
    target_global_id: str
    before_ifc_class: str | None = None
    owned_products: tuple[IfcProductEditOwnedProduct, ...] = ()


class IfcProductsEditPreflightRequest(ProductEditContract):
    targets: tuple[IfcProductsEditTarget, ...]


class IfcProductsEditPreflightResult(ProductEditContract):
    products: tuple[IfcProductEditPreflightProduct, ...]
    checks: tuple[ProductEditCheck, ...] = ()
    accepted: bool

    @model_validator(mode="after")
    def accepted_has_no_failure(self):
        if self.accepted and any(item.status == "FAIL" for item in self.checks):
            raise ValueError("accepted target preflight cannot contain a failure")
        return self


class CompileIfcProductsEditCommand(ProductEditContract):
    rows: tuple[ProductEditActionRow, ...]
    families: dict[str, ProductFamilyContract] = Field(default_factory=dict)
    preflight: IfcProductsEditPreflightResult


class IfcProductEditExecutionProduct(ProductEditContract):
    input_row_index: int = Field(ge=0)
    target_global_id: str
    before_ifc_class: str | None
    after_ifc_class: str | None = None
    result_global_id: str | None = None
    execution_status: Literal["PASSED", "FAILED", "NOT_RUN", "UNKNOWN"] = "PASSED"


class IfcProductsEditRevisionEffect(ProductEditContract):
    products: tuple[IfcProductEditExecutionProduct, ...]
    changes: tuple[ProductEditChange, ...]
    checks: tuple[ProductEditCheck, ...] = ()
    bindings: tuple[IfcProductComponentBinding, ...] = ()
    timings: dict[str, float] = Field(default_factory=dict)


class ProductsEditActionCounts(ProductEditContract):
    replace: int = Field(ge=0)
    placement: int = Field(ge=0)
    delete: int = Field(ge=0)


class ProductsEditSummary(ProductEditContract):
    input_count: int = Field(ge=0)
    committed_count: int = Field(ge=0)
    committed_by_action: ProductsEditActionCounts
    created_product_count: int = Field(ge=0)
    deleted_product_count: int = Field(ge=0)
    timings_seconds: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def coherent_counts(self):
        if (self.committed_count > self.input_count or self.committed_count != sum(
                self.committed_by_action.model_dump().values())):
            raise ValueError("committed counts must preserve the complete input denominator")
        if self.committed_count not in {0, self.input_count}:
            raise ValueError("product edits commit the complete batch atomically")
        if any(value < 0 for value in self.timings_seconds.values()):
            raise ValueError("elapsed times cannot be negative")
        return self


class ProductsEditResult(ProductEditContract):
    operation_id: str
    operation_alias: str | None = None
    outcome: Literal["prepared", "indeterminate", "succeeded", "failed"]
    summary: ProductsEditSummary | None = None
    report_package_alias: str | None = None
    ifc_revision_id: str | None = None
    error_code: str | None = None
    error_phase: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def truthful_result(self):
        if self.outcome == "succeeded":
            if self.summary is None or self.report_package_alias is None or self.ifc_revision_id is None:
                raise ValueError("successful product edit requires its original report and revision")
            if self.summary.committed_count != self.summary.input_count:
                raise ValueError("success requires every input row committed")
        elif self.ifc_revision_id is not None:
            raise ValueError("non-successful edit cannot expose a result revision")
        if self.outcome in {"prepared", "indeterminate"} and any(value is not None for value in (
                self.summary, self.report_package_alias, self.error_code, self.message)):
            raise ValueError("unsettled edit has no invented terminal result")
        if self.outcome == "failed" and self.summary is not None and any((
                self.summary.committed_count, self.summary.created_product_count, self.summary.deleted_product_count)):
            raise ValueError("failed edit reports zero actual changes")
        return self


