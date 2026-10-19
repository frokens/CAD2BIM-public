"""Direct critical-operation provider response contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import Field, StrictBool, field_validator, model_validator
from CAD2BIM_backend.domain.data_transform import DataTransformInputRead
from CAD2BIM_backend.domain.product_apply import ProductApplySummary
from CAD2BIM_backend.domain.product_edit import ProductsEditSummary
from CAD2BIM_backend.domain.product_report import ProductReportDiagnostics

from CAD2BIM_mcp.direct.ifc.preview_cases import PreviewCaseResultCard, PreviewDependencyReference
from CAD2BIM_backend.domain.preview_cases import PreviewCaseDiagnostic
from CAD2BIM_mcp.direct.cad.contracts import CadMeasurementOperationDetail
from CAD2BIM_mcp.direct.catalog.contracts import DataPackagePrimaryResult
from CAD2BIM_mcp.direct.contract_base import (
    _ERROR_CODE_PATTERN,
    DirectContract,
    NonNegativeInt64,
    PositiveInt64,
    _non_blank_no_nul,
    _utc,
    _validate_terminal_times,
)
from CAD2BIM_mcp.direct.ifc.model.contracts import (
    IfcContextOperationInput,
    IfcCoordinateReferenceChangeInput,
    IfcModelInitializeOperationDetail,
    IfcModelPrimaryResult,
    IfcProjectUnitChangeInput,
)
from CAD2BIM_mcp.direct.transform.contracts import (
    DataTransformOperationDetail,
)
from CAD2BIM_backend.domain.ifc_project import UpdateIfcProjectCommand
from CAD2BIM_backend.domain.ifc_spatial_structure import ApplyIfcSpatialStructureCommand
from CAD2BIM_mcp.tool_docs import operation as _tool_docs


class DataOperationCard(DirectContract):
    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$")
    tool: Literal["cad_import_source"] = "cad_import_source"
    outcome: Literal["succeeded", "failed"]
    source_filename: str = Field(min_length=1, max_length=255)
    started_at: datetime
    finished_at: datetime
    result_package_alias: str | None = Field(
        default=None,
        pattern=r"^data_[0-9]{3,}$",
    )
    error_code: str | None = Field(default=None, min_length=1, max_length=120)


class _OperationCard(DirectContract):
    operation_alias: str | None = Field(default=None, pattern=r"^op_[0-9]{3,}$", max_length=32)
    operation_ref: str | None = Field(default=None, min_length=1, max_length=16_384)
    sequence: PositiveInt64
    initiator: Literal["direct_client", "runner"]
    intent_summary: str = Field(min_length=1, max_length=1_303)
    started_at: datetime
    finished_at: datetime

    _intent_summary = field_validator("intent_summary")(_non_blank_no_nul)
    _started_at = field_validator("started_at")(_utc)
    _finished_at = field_validator("finished_at")(_utc)

    @model_validator(mode="after")
    def validate_times(self) -> _OperationCard:
        _validate_terminal_times(self.started_at, self.finished_at)
        return self


class SucceededSourceIngestionOperationCard(_OperationCard):
    operation_kind: Literal["source_ingestion"]
    tool_name: Literal["cad_import_source"]
    outcome: Literal["succeeded"]
    primary_result: DataPackagePrimaryResult


class FailedSourceIngestionOperationCard(_OperationCard):
    operation_kind: Literal["source_ingestion"]
    tool_name: Literal["cad_import_source"]
    outcome: Literal["failed"]
    error_code: str = Field(pattern=_ERROR_CODE_PATTERN, max_length=120)


class SucceededDataTransformOperationCard(_OperationCard):
    operation_kind: Literal["data_transform"]
    tool_name: Literal["data_derive", "data_transform", "data_repair_transform", "cad_compute_measurements"]
    outcome: Literal["succeeded"]
    primary_result: DataPackagePrimaryResult


class FailedDataTransformOperationCard(_OperationCard):
    operation_kind: Literal["data_transform"]
    tool_name: Literal["data_derive", "data_transform", "data_repair_transform", "cad_compute_measurements"]
    outcome: Literal["failed"]
    error_code: str = Field(pattern=_ERROR_CODE_PATTERN, max_length=120)


class SucceededIfcModelWriteOperationCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"]
    tool_name: Literal["ifc_model_initialize"]
    outcome: Literal["succeeded"]
    primary_result: IfcModelPrimaryResult


class FailedIfcModelWriteOperationCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"]
    tool_name: Literal["ifc_model_initialize"]
    outcome: Literal["failed"]
    error_code: str = Field(pattern=_ERROR_CODE_PATTERN, max_length=120)


class ProductReportTable(DirectContract):
    payload_name: str = Field(min_length=1, max_length=120)
    format: Literal["parquet"] = "parquet"
    row_count: NonNegativeInt64
    logical_key_fields: tuple[str, ...]


class ProductApplyReportPrimaryResult(DirectContract):
    report_package: DataPackagePrimaryResult
    report_tables: tuple[ProductReportTable, ...]
    summary: ProductApplySummary = Field(description=_tool_docs.PRODUCT_APPLY_BATCH_SUMMARY_DESCRIPTION)


class ProductApplyCommittedRevision(DirectContract):
    revision_no: PositiveInt64 = Field(
        description=_tool_docs.PRODUCT_APPLY_COMMITTED_REVISION_REVISION_NO_DESCRIPTION)
    size_bytes: PositiveInt64 = Field(
        description=_tool_docs.PRODUCT_APPLY_COMMITTED_REVISION_SIZE_BYTES_DESCRIPTION)


class CommittedProductApplyPrimaryResult(ProductApplyReportPrimaryResult):
    committed_revision: ProductApplyCommittedRevision


class ProductsEditReportPrimaryResult(DirectContract):
    report_package: DataPackagePrimaryResult
    report_tables: tuple[ProductReportTable, ...]
    summary: ProductsEditSummary


class CommittedProductsEditPrimaryResult(ProductsEditReportPrimaryResult):
    committed_revision: ProductApplyCommittedRevision


class SucceededProductsEditCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"] = "ifc_model_write"
    tool_name: Literal["ifc_products_edit"] = "ifc_products_edit"
    outcome: Literal["succeeded"] = "succeeded"
    operation_ref: str = Field(pattern=r"^cur\..+$", max_length=16_384)
    primary_result: CommittedProductsEditPrimaryResult


class FailedProductsEditCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"] = "ifc_model_write"
    tool_name: Literal["ifc_products_edit"] = "ifc_products_edit"
    outcome: Literal["failed"] = "failed"
    operation_ref: str = Field(pattern=r"^cur\..+$", max_length=16_384)
    error_code: str = Field(pattern=_ERROR_CODE_PATTERN, max_length=120)
    primary_result: ProductsEditReportPrimaryResult | None = None


class IfcTrustedBaselineLoadPrimaryResult(DirectContract):
    result: Literal["baseline_loaded"]
    committed_revision: ProductApplyCommittedRevision


class SucceededIfcTrustedBaselineLoadOperationCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"]
    tool_name: Literal["ifc_trusted_baseline_load"]
    outcome: Literal["succeeded"]
    primary_result: IfcTrustedBaselineLoadPrimaryResult


class FailedIfcTrustedBaselineLoadOperationCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"]
    tool_name: Literal["ifc_trusted_baseline_load"]
    outcome: Literal["failed"]
    error_code: str = Field(pattern=_ERROR_CODE_PATTERN, max_length=120)


class IfcTrustedBaselineLoadOperationDetail(DirectContract):
    detail_kind: Literal["ifc_trusted_baseline_load"]
    result: Literal["baseline_loaded"] | None
    code_available: Literal[False] = False


class SucceededIfcProductsApplyOperationCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"] = "ifc_model_write"
    tool_name: Literal["ifc_products_apply"] = "ifc_products_apply"
    outcome: Literal["succeeded"] = "succeeded"
    primary_result: CommittedProductApplyPrimaryResult


class FailedIfcProductsApplyOperationCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"] = "ifc_model_write"
    tool_name: Literal["ifc_products_apply"] = "ifc_products_apply"
    outcome: Literal["failed"] = "failed"
    error_code: str = Field(pattern=_ERROR_CODE_PATTERN, max_length=120)
    primary_result: ProductApplyReportPrimaryResult | None = None


class IfcGeometryBasisConfigurePrimaryResult(DirectContract):
    result: Literal["geometry_basis_configured"]


class SucceededIfcGeometryBasisConfigureOperationCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"]
    tool_name: Literal["ifc_model_configure_geometry_basis"]
    outcome: Literal["succeeded"]
    primary_result: IfcGeometryBasisConfigurePrimaryResult


class FailedIfcGeometryBasisConfigureOperationCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"]
    tool_name: Literal["ifc_model_configure_geometry_basis"]
    outcome: Literal["failed"]
    error_code: str = Field(pattern=_ERROR_CODE_PATTERN, max_length=120)


class IfcProjectUpdatePrimaryResult(DirectContract):
    result: Literal["project_updated"]


class SucceededIfcProjectUpdateOperationCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"]
    tool_name: Literal["ifc_project_update"]
    outcome: Literal["succeeded"]
    primary_result: IfcProjectUpdatePrimaryResult


class FailedIfcProjectUpdateOperationCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"]
    tool_name: Literal["ifc_project_update"]
    outcome: Literal["failed"]
    error_code: str = Field(pattern=_ERROR_CODE_PATTERN, max_length=120)


class IfcSpatialStructureApplyPrimaryResult(DirectContract):
    result: Literal["spatial_structure_applied"]
    applied_count: PositiveInt64


class SucceededIfcSpatialStructureApplyOperationCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"]
    tool_name: Literal["ifc_spatial_structure_apply"]
    outcome: Literal["succeeded"]
    primary_result: IfcSpatialStructureApplyPrimaryResult


class FailedIfcSpatialStructureApplyOperationCard(_OperationCard):
    operation_kind: Literal["ifc_model_write"]
    tool_name: Literal["ifc_spatial_structure_apply"]
    outcome: Literal["failed"]
    error_code: str = Field(pattern=_ERROR_CODE_PATTERN, max_length=120)


class AuthoringDraftPrimaryResult(DirectContract):
    result: Literal["authoring_draft_saved"] = "authoring_draft_saved"
    root_kind: Literal["geometry", "family"]
    draft_ref: str = Field(min_length=1, max_length=16_384)
    edit_token: PositiveInt64


class AuthoringPublishedGeometryReference(DirectContract):
    resource_version_alias: str = Field(min_length=1, max_length=16_384)


class AuthoringPublishPrimaryResult(DirectContract):
    result: Literal["authoring_published"] = "authoring_published"
    root_kind: Literal["geometry", "family"]
    geometry_versions: tuple[AuthoringPublishedGeometryReference, ...] = ()
    family_version_alias: str | None = Field(default=None, min_length=1, max_length=16_384)


class SucceededAuthoringPublishOperationCard(_OperationCard):
    operation_kind: Literal["authoring_publish"] = "authoring_publish"
    tool_name: Literal["ifc_authoring_publish"] = "ifc_authoring_publish"
    outcome: Literal["succeeded"] = "succeeded"
    primary_result: AuthoringPublishPrimaryResult


class FailedAuthoringPublishOperationCard(_OperationCard):
    operation_kind: Literal["authoring_publish"] = "authoring_publish"
    tool_name: Literal["ifc_authoring_publish"] = "ifc_authoring_publish"
    outcome: Literal["failed"] = "failed"
    root_kind: Literal["geometry", "family"]
    error_code: str = Field(pattern=_ERROR_CODE_PATTERN, max_length=120)


class SucceededGeometryResourceWriteOperationCard(_OperationCard):
    operation_kind: Literal["geometry_resource_write"]
    tool_name: Literal["ifc_geometry_resource_write"]
    outcome: Literal["succeeded"]
    primary_result: AuthoringDraftPrimaryResult


class ProductFamilyWritePrimaryResult(DirectContract):
    result: Literal["product_family_version_created"]


class SucceededProductFamilyWriteOperationCard(_OperationCard):
    operation_kind: Literal["product_family_write"]
    tool_name: Literal["ifc_product_family_write"]
    outcome: Literal["succeeded"]
    primary_result: AuthoringDraftPrimaryResult | ProductFamilyWritePrimaryResult


class FailedProductFamilyWriteOperationCard(_OperationCard):
    operation_kind: Literal["product_family_write"]
    tool_name: Literal["ifc_product_family_write"]
    outcome: Literal["failed"]
    error_code: str = Field(pattern=_ERROR_CODE_PATTERN, max_length=120)


class GeometryInspectionPrimaryResult(DirectContract):
    result: Literal["geometry_inspection_retained"] = "geometry_inspection_retained"
    inspection_ref: str = Field(pattern=r"^cur\..+$", max_length=16_384)
    source_kind: Literal["geometry_sample", "family_realization"]
    source_version_ref: str = Field(pattern=r"^cur\..+$", max_length=16_384)
    summary: str = Field(min_length=1, max_length=1_303)


class PreviewCaseIndex(DirectContract):
    case_key: str
    execution_status: Literal["completed", "technical_error", "not_run"]
    status: Literal["BUILT", "REJECTED", "SOLVED", "AMBIGUOUS", "INFEASIBLE"] | None = None
    expectation_check: Literal["pass", "fail", "not_checked"]
    inspection_ref: str | None = None


class PreviewCaseReportPrimaryResult(DirectContract):
    result: Literal["preview_case_report"] = "preview_case_report"
    source_kind: Literal["geometry_sample", "family_realization"]
    source_ref: str
    case_count: int = Field(ge=1, le=16)
    checks_passed: bool
    summary: str
    cases: tuple[PreviewCaseIndex, ...] = Field(default=(), max_length=16)


class SucceededGeometryInspectionOperationCard(_OperationCard):
    operation_kind: Literal["geometry_inspection"]
    tool_name: Literal["ifc_geometry_preview", "ifc_product_realization_preview"]
    outcome: Literal["succeeded"]
    primary_result: GeometryInspectionPrimaryResult | PreviewCaseReportPrimaryResult

    @model_validator(mode="after")
    def validate_source_tool(self) -> SucceededGeometryInspectionOperationCard:
        expected = (
            "ifc_geometry_preview" if self.primary_result.source_kind == "geometry_sample"
            else "ifc_product_realization_preview"
        )
        if self.tool_name != expected:
            raise ValueError("retained result source must match its Preview tool")
        return self


class FailedGeometryInspectionOperationCard(_OperationCard):
    operation_kind: Literal["geometry_inspection"]
    tool_name: Literal["ifc_geometry_preview", "ifc_product_realization_preview"]
    outcome: Literal["failed"]
    error_code: str = Field(pattern=_ERROR_CODE_PATTERN, max_length=120)
    primary_result: PreviewCaseReportPrimaryResult | None = None


SucceededOperationCard: TypeAlias = (
    SucceededSourceIngestionOperationCard
    | SucceededDataTransformOperationCard
    | SucceededIfcModelWriteOperationCard
    | SucceededIfcTrustedBaselineLoadOperationCard
    | SucceededIfcProductsApplyOperationCard
    | SucceededProductsEditCard
    | SucceededIfcGeometryBasisConfigureOperationCard
    | SucceededIfcProjectUpdateOperationCard
    | SucceededIfcSpatialStructureApplyOperationCard
    | SucceededGeometryResourceWriteOperationCard
    | SucceededProductFamilyWriteOperationCard
    | SucceededAuthoringPublishOperationCard
    | SucceededGeometryInspectionOperationCard
)
FailedOperationCard: TypeAlias = (
    FailedAuthoringPublishOperationCard
    | FailedSourceIngestionOperationCard
    | FailedDataTransformOperationCard
    | FailedIfcModelWriteOperationCard
    | FailedIfcTrustedBaselineLoadOperationCard
    | FailedIfcProductsApplyOperationCard
    | FailedProductsEditCard
    | FailedIfcGeometryBasisConfigureOperationCard
    | FailedIfcProjectUpdateOperationCard
    | FailedIfcSpatialStructureApplyOperationCard
    | FailedProductFamilyWriteOperationCard
    | FailedGeometryInspectionOperationCard
)
OperationCard: TypeAlias = SucceededOperationCard | FailedOperationCard


class OperationSummaryCard(_OperationCard):
    # Public discovery/recovery guidance belongs to tool_docs.operation.

    operation_kind: str
    tool_name: str
    outcome: Literal["succeeded", "failed"]
    error_code: str | None = Field(default=None, pattern=_ERROR_CODE_PATTERN, max_length=120)


class OperationListResult(DirectContract):
    operations: tuple[OperationSummaryCard, ...] = Field(max_length=50)
    complete: StrictBool
    next_before_sequence: PositiveInt64 | None

    @model_validator(mode="after")
    def validate_page(self) -> OperationListResult:
        sequences = [item.sequence for item in self.operations]
        aliases = [item.operation_alias for item in self.operations]
        if len(set(sequences)) != len(sequences) or len(set(aliases)) != len(aliases):
            raise ValueError("Operation page aliases and sequences must be unique")
        if any(left <= right for left, right in zip(sequences, sequences[1:])):
            raise ValueError("Operation page sequences must be strictly descending")
        if self.complete:
            if self.next_before_sequence is not None:
                raise ValueError("complete Operation page must not have a continuation")
        else:
            if not sequences or self.next_before_sequence != min(sequences):
                raise ValueError(
                    "incomplete Operation page must continue from its minimum sequence"
                )
        return self


class IfcProductsRecoveryCard(DirectContract):
    __doc__ = _tool_docs.IFC_APPLY_RECOVERY_CARD_DESCRIPTION

    operation_ref: str = Field(min_length=1, max_length=16_384)
    operation_alias: str | None = Field(default=None, pattern=r"^op_[0-9]{3,}$", max_length=32)
    tool_name: Literal["ifc_products_apply", "ifc_products_edit"]
    outcome: Literal["prepared", "indeterminate", "succeeded", "failed"]
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = Field(default=None, pattern=_ERROR_CODE_PATTERN, max_length=120)


class IfcProductsRecoveryListResult(DirectContract):
    operations: tuple[IfcProductsRecoveryCard, ...] = Field(max_length=50)
    complete: StrictBool
    next_cursor: str | None = Field(default=None, pattern=r"^cur\..+$", max_length=16_384)


class SourceIngestionSourceDetail(DirectContract):
    filename: str = Field(min_length=1, max_length=255)
    media_type: Literal["application/dxf"]
    size_bytes: PositiveInt64 | None = Field(
        description=(_tool_docs.SOURCE_INGESTION_SOURCE_DETAIL_SIZE_BYTES_DESCRIPTION)
    )

    _filename = field_validator("filename")(_non_blank_no_nul)


class SourceIngestionOperationDetail(DirectContract):
    detail_kind: Literal["source_ingestion"]
    source: SourceIngestionSourceDetail
    code_available: Literal[False]


class IfcGeometryBasisConfigureRequest(DirectContract):
    project_unit_change: IfcProjectUnitChangeInput | None = None
    coordinate_reference_change: IfcCoordinateReferenceChangeInput | None = None
    context_operations: tuple[IfcContextOperationInput, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def require_nonempty_change(self) -> IfcGeometryBasisConfigureRequest:
        if (
            self.project_unit_change is None
            and self.coordinate_reference_change is None
            and not self.context_operations
        ):
            raise ValueError("geometry basis request must contain at least one change")
        return self


class IfcGeometryBasisConfigureOperationDetail(DirectContract):
    detail_kind: Literal["ifc_model_configure_geometry_basis"]
    request: IfcGeometryBasisConfigureRequest
    result: Literal["geometry_basis_configured"] | None


class IfcProjectUpdateOperationDetail(DirectContract):
    detail_kind: Literal["ifc_project_update"]
    request: UpdateIfcProjectCommand
    result: Literal["project_updated"] | None


class IfcSpatialStructureApplyOperationDetail(DirectContract):
    detail_kind: Literal["ifc_spatial_structure_apply"]
    request: ApplyIfcSpatialStructureCommand
    result: Literal["spatial_structure_applied"] | None
    applied_count: PositiveInt64 | None

    @model_validator(mode="after")
    def validate_result_count(self) -> "IfcSpatialStructureApplyOperationDetail":
        if (self.result is None) != (self.applied_count is None):
            raise ValueError("spatial result and applied_count must be present together")
        if self.applied_count is not None and self.applied_count != len(self.request.operations):
            raise ValueError("applied_count must equal the persisted operation count")
        return self


class ProductApplyInputBinding(DirectContract):
    package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)
    payload_name: str = Field(min_length=1, max_length=120)
    logical_key_fields: tuple[str, ...] = Field(max_length=16)


class IfcProductsApplyOperationDetail(DirectContract):
    detail_kind: Literal["ifc_products_apply"] = "ifc_products_apply"
    family_version_ref: str = Field(min_length=1, max_length=16_384)
    input_binding: ProductApplyInputBinding
    diagnostics: ProductReportDiagnostics | None = None


class ProductsEditDetail(DirectContract):
    detail_kind: Literal["ifc_products_edit"] = "ifc_products_edit"
    input_binding: ProductApplyInputBinding
    base_revision_no: PositiveInt64
    diagnostics: ProductReportDiagnostics | None = None


class UnsettledIfcProductsApplyCard(IfcProductsRecoveryCard):
    tool_name: Literal["ifc_products_apply"] = "ifc_products_apply"
    outcome: Literal["prepared", "indeterminate"]


class UnsettledIfcProductsApplyResult(DirectContract):
    operation: UnsettledIfcProductsApplyCard
    detail: IfcProductsApplyOperationDetail


class UnsettledProductsEditCard(IfcProductsRecoveryCard):
    tool_name: Literal["ifc_products_edit"] = "ifc_products_edit"
    outcome: Literal["prepared", "indeterminate"]


class UnsettledProductsEditResult(DirectContract):
    operation: UnsettledProductsEditCard
    detail: ProductsEditDetail


class ProductFamilyWriteOperationDetail(DirectContract):
    detail_kind: Literal["product_family_write"]
    mode: Literal["system_copy"]
    display_name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=2_048)
    product_structure_kind: Literal["single_product", "semantic_assembly"]
    component_count: PositiveInt64
    geometry_fragment_count: PositiveInt64
    interface_count: NonNegativeInt64
    geometry_binding_count: PositiveInt64
    program_entry: Literal["main.py:realize"]
    code_available: Literal[True]
    source_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    _display_name = field_validator("display_name")(_non_blank_no_nul)
    _purpose = field_validator("purpose")(_non_blank_no_nul)


class GeometryInspectionOperationDetail(DirectContract):
    detail_kind: Literal["geometry_inspection"] = "geometry_inspection"
    source_kind: Literal["geometry_sample", "family_realization"]
    source_version_ref: str = Field(pattern=r"^cur\..+$", max_length=16_384)
    request_json: str = Field(
        min_length=1,
        description=_tool_docs.GEOMETRY_INSPECTION_REQUEST_JSON_DESCRIPTION,
    )
    inspection_ref: str | None = Field(
        default=None, pattern=r"^cur\..+$", max_length=16_384,
    )




class AuthoringDraftOperationDetail(AuthoringDraftPrimaryResult):
    detail_kind: Literal["authoring_draft_write"] = "authoring_draft_write"
    code_available: Literal[True] = True


class AuthoringPreviewExample(DirectContract):
    availability: Literal["available"] = "available"
    operation_ref: str = Field(min_length=1, max_length=16_384)
    operation_alias: str | None = Field(default=None, pattern=r"^op_[0-9]{3,}$")
    cases: tuple[PreviewCaseIndex, ...] = Field(max_length=16)


class UnavailableAuthoringPreviewExample(DirectContract):
    availability: Literal["unavailable"] = "unavailable"


class AuthoringPublishOperationDetail(AuthoringPublishPrimaryResult):
    detail_kind: Literal["authoring_publish"] = "authoring_publish"
    preview_examples: tuple[AuthoringPreviewExample | UnavailableAuthoringPreviewExample, ...] = ()


class FailedAuthoringPublishOperationDetail(DirectContract):
    detail_kind: Literal["authoring_publish"] = "authoring_publish"
    root_kind: Literal["geometry", "family"]


OperationDetail: TypeAlias = (
    SourceIngestionOperationDetail
    | DataTransformOperationDetail
    | CadMeasurementOperationDetail
    | IfcModelInitializeOperationDetail
    | IfcTrustedBaselineLoadOperationDetail
    | IfcGeometryBasisConfigureOperationDetail
    | IfcProjectUpdateOperationDetail
    | IfcSpatialStructureApplyOperationDetail
    | IfcProductsApplyOperationDetail
    | ProductsEditDetail
    | AuthoringDraftOperationDetail
    | AuthoringPublishOperationDetail
    | ProductFamilyWriteOperationDetail
    | GeometryInspectionOperationDetail
    | FailedAuthoringPublishOperationDetail
)


def _expected_detail_kinds(tool_name: str) -> frozenset[str]:
    return {
        "cad_import_source": frozenset({"source_ingestion"}),
        "data_transform": frozenset({"data_transform"}),
        "data_derive": frozenset({"data_transform"}),
        "data_repair_transform": frozenset({"data_transform"}),
        "cad_compute_measurements": frozenset({"cad_measurement"}),
        "ifc_model_initialize": frozenset({"ifc_model_initialize"}),
        "ifc_trusted_baseline_load": frozenset({"ifc_trusted_baseline_load"}),
        "ifc_model_configure_geometry_basis": frozenset({"ifc_model_configure_geometry_basis"}),
        "ifc_project_update": frozenset({"ifc_project_update"}),
        "ifc_spatial_structure_apply": frozenset({"ifc_spatial_structure_apply"}),
        "ifc_products_apply": frozenset({"ifc_products_apply"}),
        "ifc_products_edit": frozenset({"ifc_products_edit"}),
        "ifc_geometry_resource_write": frozenset({"authoring_draft_write"}),
        "ifc_product_family_write": frozenset({"product_family_write", "authoring_draft_write"}),
        "ifc_authoring_publish": frozenset({"authoring_publish"}),
        "ifc_geometry_preview": frozenset({"geometry_inspection", "preview_case_report"}),
        "ifc_product_realization_preview": frozenset({"geometry_inspection", "preview_case_report"}),
    }[tool_name]


class OperationErrorDetail(DirectContract):
    code: str = Field(pattern=_ERROR_CODE_PATTERN, max_length=120)
    phase: Literal[
        "source_extraction",
        "code_validation",
        "execution",
        "output_validation",
        "ifc_validation",
        "storage_verification",
        "commit_fence",
    ]
    message: str = Field(min_length=1, max_length=2_000)
    retryable: Literal[False]

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 2_000:
            raise ValueError("error message exceeds 2,000 UTF-8 bytes")
        return _non_blank_no_nul(value)


class SucceededOperationGetResult(DirectContract):
    operation: SucceededOperationCard
    detail: OperationDetail

    @model_validator(mode="after")
    def validate_kind(self) -> SucceededOperationGetResult:
        if isinstance(self.detail, ProductsEditDetail):
            if (not isinstance(self.operation, SucceededProductsEditCard)
                    or self.operation.primary_result.summary.committed_count != self.operation.primary_result.summary.input_count):
                raise ValueError("successful edit requires its original committed whole-batch summary")
        if isinstance(self.detail, IfcProductsApplyOperationDetail):
            if (not isinstance(self.operation, SucceededIfcProductsApplyOperationCard)
                    or self.operation.primary_result.summary.outcome != "committed"):
                raise ValueError("successful Apply projection must contain its committed report")
        if isinstance(self.detail, (AuthoringDraftOperationDetail, AuthoringPublishOperationDetail)):
            actual = self.detail.model_dump(exclude={"detail_kind", "code_available", "preview_examples"})
            if actual != self.operation.primary_result.model_dump():
                raise ValueError("authoring detail differs from its original Operation result")
        if self.detail.detail_kind not in _expected_detail_kinds(self.operation.tool_name):
            raise ValueError("Operation tool_name and detail kind must match")
        if isinstance(self.detail, IfcModelInitializeOperationDetail):
            if (
                self.detail.result != "model_initialized"
                or not isinstance(self.operation, SucceededIfcModelWriteOperationCard)
                or self.operation.primary_result.project_global_id != self.detail.project_global_id
                or self.operation.primary_result.project_name != self.detail.project_name
            ):
                raise ValueError("successful IFC initialization projection is inconsistent")
        if isinstance(self.detail, IfcTrustedBaselineLoadOperationDetail) and (
            self.detail.result != "baseline_loaded"
            or not isinstance(self.operation, SucceededIfcTrustedBaselineLoadOperationCard)
        ):
            raise ValueError("successful trusted baseline projection is inconsistent")
        if isinstance(self.detail, IfcGeometryBasisConfigureOperationDetail) and (
            self.detail.result != "geometry_basis_configured"
            or not isinstance(self.operation, SucceededIfcGeometryBasisConfigureOperationCard)
        ):
            raise ValueError("successful geometry-basis projection is inconsistent")
        if isinstance(self.detail, IfcProjectUpdateOperationDetail) and (
            self.detail.result != "project_updated"
            or not isinstance(self.operation, SucceededIfcProjectUpdateOperationCard)
        ):
            raise ValueError("successful Project update projection is inconsistent")
        if isinstance(self.detail, IfcSpatialStructureApplyOperationDetail) and (
            self.detail.result != "spatial_structure_applied"
            or self.detail.applied_count is None
            or not isinstance(self.operation, SucceededIfcSpatialStructureApplyOperationCard)
            or self.operation.primary_result.applied_count != self.detail.applied_count
        ):
            raise ValueError("successful spatial-structure projection is inconsistent")
        if isinstance(self.detail, ProductFamilyWriteOperationDetail) and (
            not isinstance(self.operation, SucceededProductFamilyWriteOperationCard)
        ):
            raise ValueError("successful ProductFamily projection is inconsistent")
        if isinstance(self.detail, GeometryInspectionOperationDetail) and (
            not isinstance(self.operation, SucceededGeometryInspectionOperationCard)
            or not isinstance(self.operation.primary_result, GeometryInspectionPrimaryResult)
            or self.detail.inspection_ref != self.operation.primary_result.inspection_ref
            or self.detail.source_kind != self.operation.primary_result.source_kind
            or self.detail.source_version_ref != self.operation.primary_result.source_version_ref
        ):
            raise ValueError("successful retained-result projection is inconsistent")
        return self


class FailedOperationGetResult(DirectContract):
    operation: FailedOperationCard
    detail: OperationDetail
    error: OperationErrorDetail

    @model_validator(mode="after")
    def validate_failure(self) -> FailedOperationGetResult:
        if isinstance(self.detail, ProductsEditDetail):
            if not isinstance(self.operation, FailedProductsEditCard):
                raise ValueError("failed edit detail requires its original edit card")
            primary = self.operation.primary_result
            if primary is not None and any((primary.summary.committed_count,
                    primary.summary.created_product_count, primary.summary.deleted_product_count)):
                raise ValueError("failed edit cannot project committed changes")
        if isinstance(self.detail, IfcProductsApplyOperationDetail):
            if (not isinstance(self.operation, FailedIfcProductsApplyOperationCard)
                    or (self.operation.primary_result is not None
                        and self.operation.primary_result.summary.outcome == "committed")):
                raise ValueError("failed Apply cannot project committed products")
        if self.detail.detail_kind not in _expected_detail_kinds(self.operation.tool_name):
            raise ValueError("Operation tool_name and detail kind must match")
        if self.operation.error_code != self.error.code:
            raise ValueError("Operation card and error codes must match")
        if isinstance(self.detail, IfcModelInitializeOperationDetail) and (
            self.detail.result is not None
            or not isinstance(self.operation, FailedIfcModelWriteOperationCard)
        ):
            raise ValueError("failed IFC initialization projection is inconsistent")
        if isinstance(self.detail, IfcTrustedBaselineLoadOperationDetail) and (
            self.detail.result is not None
            or not isinstance(self.operation, FailedIfcTrustedBaselineLoadOperationCard)
        ):
            raise ValueError("failed trusted baseline projection is inconsistent")
        if isinstance(self.detail, IfcGeometryBasisConfigureOperationDetail) and (
            self.detail.result is not None
            or not isinstance(self.operation, FailedIfcGeometryBasisConfigureOperationCard)
        ):
            raise ValueError("failed geometry-basis projection is inconsistent")
        if isinstance(self.detail, IfcProjectUpdateOperationDetail) and (
            self.detail.result is not None
            or not isinstance(self.operation, FailedIfcProjectUpdateOperationCard)
        ):
            raise ValueError("failed Project update projection is inconsistent")
        if isinstance(self.detail, IfcSpatialStructureApplyOperationDetail) and (
            self.detail.result is not None
            or self.detail.applied_count is not None
            or not isinstance(self.operation, FailedIfcSpatialStructureApplyOperationCard)
        ):
            raise ValueError("failed spatial-structure projection is inconsistent")
        if isinstance(self.detail, ProductFamilyWriteOperationDetail) and (
            not isinstance(self.operation, FailedProductFamilyWriteOperationCard)
        ):
            raise ValueError("failed ProductFamily projection is inconsistent")
        if isinstance(self.detail, GeometryInspectionOperationDetail):
            expected = (
                "ifc_geometry_preview" if self.detail.source_kind == "geometry_sample"
                else "ifc_product_realization_preview"
            )
            if (
                not isinstance(self.operation, FailedGeometryInspectionOperationCard)
                or self.detail.inspection_ref is not None
                or self.operation.tool_name != expected
            ):
                raise ValueError("failed retained-result projection is inconsistent")
        if self.operation.operation_kind == "source_ingestion":
            if self.error.phase != "source_extraction":
                raise ValueError("Source Ingestion failure must use source_extraction phase")
        elif self.operation.operation_kind == "data_transform" and self.error.phase not in {
            "code_validation",
            "execution",
            "output_validation",
            "commit_fence",
        }:
            raise ValueError("Data Transform failure has an invalid terminal phase")
        elif self.operation.operation_kind == "ifc_model_write" and self.error.phase not in {
            "ifc_validation",
            "storage_verification",
            "commit_fence",
        }:
            raise ValueError("IFC model write failure has an invalid terminal phase")
        elif (
            self.operation.operation_kind == "geometry_resource_write"
            and self.error.phase
            not in {
                "code_validation",
                "execution",
                "output_validation",
                "storage_verification",
                "commit_fence",
            }
        ):
            raise ValueError("Geometry resource write failure has an invalid terminal phase")
        elif self.operation.operation_kind == "product_family_write" and self.error.phase not in {
            "code_validation",
            "execution",
            "output_validation",
            "storage_verification",
            "commit_fence",
        }:
            raise ValueError("ProductFamily write failure has an invalid terminal phase")
        elif self.operation.operation_kind == "geometry_inspection" and self.error.phase not in {
            "code_validation", "execution", "output_validation",
            "storage_verification", "commit_fence",
        }:
            raise ValueError("retained-result failure has an invalid terminal phase")
        return self


from CAD2BIM_backend.domain.preview_report_read import PreviewOperationRead

OperationGetResult: TypeAlias = PreviewOperationRead | SucceededOperationGetResult | FailedOperationGetResult | DataTransformInputRead | UnsettledIfcProductsApplyResult | UnsettledProductsEditResult
