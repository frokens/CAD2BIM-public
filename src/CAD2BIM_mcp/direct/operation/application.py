"""Direct application behavior owned by this capability family."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogError
from CAD2BIM_backend.domain.ifc_model import ExactIfcRevision

from CAD2BIM_backend.domain.data_runtime import (
    AuthoringDraftWriteOperationDetail as DomainAuthoringDraftWriteOperationDetail,
    AuthoringPublishOperationDetail as DomainAuthoringPublishOperationDetail,
    CadMeasurementOperationDetail as DomainCadMeasurementOperationDetail,
    CriticalOperationKind,
    CriticalOperationStatus,
    PackageMemberKind,
    DataTransformOperationDetail as DomainDataTransformOperationDetail,
    IfcGeometryBasisConfigureOperationDetail as DomainIfcGeometryBasisConfigureOperationDetail,
    IfcModelInitializeOperationDetail as DomainIfcModelInitializeOperationDetail,
    IfcTrustedBaselineLoadOperationDetail as DomainIfcTrustedBaselineLoadOperationDetail,
    IfcProjectUpdateOperationDetail as DomainIfcProjectUpdateOperationDetail,
    IfcSpatialStructureApplyOperationDetail as DomainIfcSpatialStructureApplyOperationDetail,
    IfcProductsApplyOperationDetail as DomainIfcProductsApplyOperationDetail,
    IfcProductsEditOperationDetail as DomainIfcProductsEditOperationDetail,
    GeometryInspectionOperationDetail as DomainGeometryInspectionOperationDetail,
    ProductFamilyWriteOperationDetail as DomainProductFamilyWriteOperationDetail,
    PublishedCriticalOperation,
    PublishedDataPackage,
    SourceIngestionOperationDetail as DomainSourceIngestionOperationDetail,
)
from CAD2BIM_mcp.direct.cad.contracts import (
    CadMeasurementOperationDetail,
)
from CAD2BIM_mcp.direct.transform.contracts import (
    DataTransformOperationDetail,
    JsonOperationOutputDeclaration,
    ParquetOperationOutputDeclaration,
)
from CAD2BIM_backend.domain.preview_report_read import PreviewOperationRead
from CAD2BIM_mcp.direct.operation.contracts import (
    OperationSummaryCard,
    AuthoringDraftPrimaryResult,
    AuthoringPublishPrimaryResult,
    AuthoringDraftOperationDetail,
    AuthoringPublishOperationDetail,
    FailedAuthoringPublishOperationCard,
    FailedAuthoringPublishOperationDetail,
    AuthoringPreviewExample,
    SucceededAuthoringPublishOperationCard,
    PreviewCaseReportPrimaryResult,
    FailedDataTransformOperationCard,
    FailedIfcGeometryBasisConfigureOperationCard,
    FailedIfcModelWriteOperationCard,
    FailedIfcTrustedBaselineLoadOperationCard,
    FailedIfcProjectUpdateOperationCard,
    FailedIfcSpatialStructureApplyOperationCard,
    FailedIfcProductsApplyOperationCard,
    SucceededIfcProductsApplyOperationCard,
    IfcProductsApplyOperationDetail,
    ProductApplyInputBinding,
    ProductReportTable,
    ProductApplyReportPrimaryResult,
    CommittedProductApplyPrimaryResult,
    ProductApplyCommittedRevision,
    FailedProductFamilyWriteOperationCard,
    FailedGeometryInspectionOperationCard,
    FailedOperationGetResult,
    FailedSourceIngestionOperationCard,
    OperationErrorDetail,
    OperationGetResult,
    OperationListResult,
    IfcGeometryBasisConfigureOperationDetail,
    IfcGeometryBasisConfigurePrimaryResult,
    IfcTrustedBaselineLoadOperationDetail,
    IfcTrustedBaselineLoadPrimaryResult,
    IfcGeometryBasisConfigureRequest,
    IfcProjectUpdateOperationDetail,
    IfcProjectUpdatePrimaryResult,
    IfcSpatialStructureApplyOperationDetail,
    IfcSpatialStructureApplyPrimaryResult,
    ProductFamilyWriteOperationDetail,
    ProductFamilyWritePrimaryResult,
    GeometryInspectionOperationDetail,
    GeometryInspectionPrimaryResult,
    SourceIngestionOperationDetail,
    SourceIngestionSourceDetail,
    SucceededDataTransformOperationCard,
    SucceededIfcGeometryBasisConfigureOperationCard,
    SucceededIfcModelWriteOperationCard,
    SucceededIfcTrustedBaselineLoadOperationCard,
    SucceededIfcProjectUpdateOperationCard,
    SucceededIfcSpatialStructureApplyOperationCard,
    SucceededGeometryResourceWriteOperationCard,
    SucceededProductFamilyWriteOperationCard,
    SucceededGeometryInspectionOperationCard,
    SucceededOperationGetResult,
    SucceededSourceIngestionOperationCard,
)
from CAD2BIM_mcp.direct.ifc.model.contracts import (
    IfcModelInitializeOperationDetail,
    IfcModelPrimaryResult,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.directory_budget import bounded_keyset_directory

from CAD2BIM_mcp.direct.application_support import (
    _direct_error,
)
from CAD2BIM_mcp.direct.catalog.application import (
    _primary_result,
)
from CAD2BIM_backend.application.geometry_authoring_service import (
    GeometryAuthoringApplicationError,
)
from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError
from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionOperationResult
from CAD2BIM_backend.ports.reference_index import ReferenceIndexError
from CAD2BIM_mcp.direct.transform.common_inputs import DirectOperationGetInput

if TYPE_CHECKING:
    from CAD2BIM_backend.application.data_runtime_service import (
        OperationInspection,
    )


_OPERATION_GET_RESPONSE_BYTES = 64 * 1024


class OperationApplicationMixin:
    async def list_ifc_products_recovery_operations(self, direct_session_alias, *, tool_name, cursor=None, page_size=20):
        from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError
        try:
            return await self._run_runtime_read(direct_session_alias, "operation_list",
                lambda scope: list_ifc_products_recovery(scope, data_runtime=self._application,
                    references=self.reference_service, tool_name=tool_name, cursor=cursor, page_size=page_size))
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc) from None
        except ReferenceIndexError as exc:
            raise DirectApplicationError("CURSOR_INVALID", str(exc), phase="read") from None

    async def _publication_navigation(self, scope, *, root_kind, version_id):
        from CAD2BIM_mcp.direct.contract_base import OperationNavigation
        return TypeAdapter(OperationNavigation).validate_python(
            await self._require_geometry_authoring_service().publication_navigation(
                scope, root_kind=root_kind, version_id=version_id))

    async def _authoring_operation_result(self, scope, published, *, include_examples=False):
        if isinstance(published.operation.detail, DomainAuthoringPublishOperationDetail):
            if published.operation.status is not CriticalOperationStatus.SUCCEEDED:
                return None
            result = await self._require_geometry_authoring_service().operation_result(scope, published)
            if include_examples:
                examples = []
                for selection in published.operation.detail.preview_examples:
                    from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError
                    try:
                        origin = await self._application.get_operation_by_locator(scope,
                            origin_run_id=selection.origin_run_id, operation_id=selection.operation_id)
                    except DataRuntimeApplicationError as exc:
                        if exc.code != "OPERATION_NOT_FOUND":
                            raise
                        examples.append({"availability": "unavailable"})
                        continue
                    preview = await self._require_geometry_inspection_service().operation_result(scope, origin.operation)
                    keys = set(selection.case_keys)
                    cases = tuple(item for item in preview.case_index if not keys or item["case_key"] in keys)
                    if keys and keys != {item["case_key"] for item in cases}:
                        raise ValueError("Publication example cases do not match the saved report")
                    examples.append(AuthoringPreviewExample(operation_ref=self.reference_service.operation_reference(
                        scope.identity, origin_run_id=selection.origin_run_id, operation_id=selection.operation_id),
                        operation_alias=origin.operation.catalog_entry.alias if selection.origin_run_id == scope.run_id else None,
                        cases=cases).model_dump(mode="json"))
                result = {**result, "preview_examples": examples}
            return result
        if isinstance(published.operation.detail, (DomainAuthoringDraftWriteOperationDetail, DomainAuthoringPublishOperationDetail)):
            return await self._require_geometry_authoring_service().operation_result(scope, published)
        return None

    async def list_operations(
        self,
        direct_session_alias: str,
        before_sequence: int | None = None,
        page_size: int = 20,
        operation_kind: CriticalOperationKind | None = None,
        *, tool_name: str | None = None, outcome: str | None = None,
    ) -> OperationListResult:
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )

        try:
            async def read_catalog(scope):
                catalog = await self._application.list_operation_catalog(
                    scope, before_sequence=before_sequence, page_size=page_size,
                    operation_kind=operation_kind,
                    tool_name=tool_name, outcome=CriticalOperationStatus(outcome) if outcome else None,
                )
                references = {item.operation.operation_id: self.reference_service.operation_reference(
                    scope.identity, origin_run_id=item.operation.run_id, operation_id=item.operation.operation_id)
                    for item in catalog.operations} if self.reference_service else {}
                return catalog, references

            catalog, references = await self._run_runtime_read(
                direct_session_alias,
                "operation_list",
                read_catalog,
            )
            cards = tuple(
                _operation_summary_card(
                    operation,
                    operation_ref=references.get(operation.operation.operation_id),
                )
                for operation in catalog.operations
            )
            return _bounded_operation_catalog_result(
                cards,
                complete=catalog.complete,
                next_before_sequence=catalog.next_before_sequence,
            )
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc) from None
        except ProductFamilyCatalogError as exc:
            raise DirectApplicationError(exc.code, str(exc), phase="read", retryable=False) from None
        except GeometryInspectionApplicationError as exc:
            raise DirectApplicationError(
                exc.code, str(exc), phase=exc.phase, retryable=exc.retryable,
                operation_alias=exc.operation_alias,
            ) from None
        except (KeyError, TypeError, ValueError):
            raise DirectApplicationError(
                "CATALOG_INTEGRITY_ERROR",
                "The Operation directory could not be projected",
            ) from None

    async def get_operation(
        self,
        direct_session_alias: str,
        operation_alias: str | None = None,
        *, case_key: str | None = None, after_case_key: str | None = None, page_size: int = 1,
        selection: DirectOperationGetInput | None = None,
    ) -> OperationGetResult:
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )
        observed_record, observed_reference = None, None
        try:
            if selection is not None and selection.mode == "transform_input":
                return await self._run_runtime_read(
                    direct_session_alias, "operation_get",
                    lambda scope: self._application.get_transform_input(
                        scope, selection.operation_alias,
                        **selection.model_dump(exclude={"mode", "operation_alias", "direct_session_alias"}),
                    ),
                )

            async def inspect(scope):
                nonlocal case_key, after_case_key, page_size
                selected_alias = operation_alias
                origin = None
                if selection is not None:
                    if selection.mode == "inspection":
                        loaded = await self._require_geometry_inspection_service().read(scope, selection.inspection_ref)
                        origin = (loaded.manifest.origin_run_id, loaded.manifest.operation_id)
                        case_key = loaded.manifest.case_key
                    else:
                        if selection.mode != "preview_report":
                            case_key, after_case_key, page_size = selection.case_key, selection.after_case_key, selection.page_size
                        if selection.mode in {"operation_reference", "preview_report"}:
                            if self.reference_service is None:
                                raise DirectApplicationError("OPERATION_NOT_FOUND", "Operation reference support is unavailable")
                            origin = self.reference_service.operation_locator(scope.identity, selection.operation_ref)
                        else:
                            selected_alias = selection.operation_alias
                try:
                    if origin is None:
                        inspection = await self._application.get_operation(scope, selected_alias)
                    else:
                        inspection = await self._application.get_operation_by_locator(scope,
                            origin_run_id=origin[0], operation_id=origin[1])
                except DataRuntimeApplicationError as exc:
                    if getattr(exc, "operation_id", None) is not None and self.reference_service is not None:
                        exc.operation_ref = self.reference_service.operation_reference(scope.identity,
                            origin_run_id=origin[0] if origin is not None else scope.run_id,
                            operation_id=exc.operation_id)
                    raise
                inspection_result = None
                authoring_result = await self._authoring_operation_result(scope, inspection.operation, include_examples=True)
                if inspection.operation.operation.operation_kind is CriticalOperationKind.GEOMETRY_INSPECTION:
                    inspection_result = await self._require_geometry_inspection_service().operation_result(
                        scope, inspection.operation, include_request=True,
                    )
                if selection is not None and selection.mode == "inspection":
                    report = inspection_result.case_report if inspection_result else None
                    if case_key is not None:
                        case = next((item for item in report.cases if item.case.case_key == case_key), None) if report else None
                        if case is None or case.inspection_artifact != loaded.artifact:
                            raise DirectApplicationError("INSPECTION_INTEGRITY_FAILED", "Saved model does not match its original case")
                    elif inspection_result is None or inspection_result.inspection_ref != selection.inspection_ref:
                        raise DirectApplicationError("INSPECTION_INTEGRITY_FAILED", "Saved model does not match its original Operation")
                record = inspection.operation.operation
                if (inspection_result is not None and inspection_result.result_kind == "case_report"):
                    return await self._require_geometry_inspection_service().read_preview_operation(
                        scope, inspection.operation, references=self.reference_service,
                        case_key=case_key, after_case_key=after_case_key, page_size=page_size,
                        selection=selection if selection is not None and selection.mode == "preview_report" else None,
                        saved=inspection_result)
                if selection is not None and selection.mode == "preview_report":
                    raise GeometryInspectionApplicationError("PREVIEW_REPORT_NOT_AVAILABLE", "The original Operation has no saved Preview case report", phase="read")
                code_locator = None
                if isinstance(record.detail, DomainDataTransformOperationDetail):
                    code_locator = self._require_code_discovery_service().data_operation_locator(scope, inspection.operation)
                family_version_ref = None
                if isinstance(record.detail, DomainIfcProductsApplyOperationDetail):
                    family_version_ref = await self._require_product_family_catalog_service().version_ref_for_visible_id(
                        scope.identity, record.detail.request.family_version_id, verify_assets=False)
                reference = self.reference_service.operation_reference(scope.identity,
                    origin_run_id=record.run_id, operation_id=record.operation_id) if self.reference_service else None
                return inspection, inspection_result, authoring_result, reference, record.run_id == scope.run_id, family_version_ref, code_locator

            read_result = await self._run_runtime_read(
                direct_session_alias,
                "operation_get",
                inspect,
            )
            if isinstance(read_result, PreviewOperationRead):
                return read_result
            inspection, inspection_result, authoring_result, reference, same_run, family_version_ref, code_locator = read_result
            observed_record, observed_reference = inspection.operation.operation, reference
            if isinstance(inspection.operation.operation.detail, DomainIfcProductsApplyOperationDetail):
                return project_ifc_apply_operation(inspection, family_version_ref=family_version_ref,
                    operation_ref=reference, same_run=same_run)
            if isinstance(inspection.operation.operation.detail, DomainIfcProductsEditOperationDetail):
                return project_ifc_products_edit_operation(inspection, operation_ref=reference, same_run=same_run)
            return _operation_get_result(
                inspection, inspection_result=inspection_result,
                authoring_result=authoring_result,
                case_key=case_key, after_case_key=after_case_key, page_size=page_size,
                operation_ref=reference, same_run=same_run,
                family_version_ref=family_version_ref,
                code_locator=code_locator,
            )
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc) from None
        except ProductFamilyCatalogError as exc:
            raise DirectApplicationError(exc.code, str(exc), phase="read", retryable=False) from None
        except ReferenceIndexError as exc:
            raise DirectApplicationError("OPERATION_NOT_FOUND", str(exc), phase="read", retryable=False) from None
        except (GeometryAuthoringApplicationError, GeometryInspectionApplicationError) as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase=exc.phase,
                retryable=exc.retryable,
                operation_alias=exc.operation_alias,
                operation_ref=getattr(exc, "operation_ref", None),
                operation_status=getattr(exc, "operation_status", None),
            ) from None
        except (KeyError, TypeError, ValueError, ValidationError):
            raise DirectApplicationError(
                "OPERATION_INTEGRITY_ERROR",
                "The Operation detail could not be projected",
                operation_ref=observed_reference,
                operation_alias=(inspection.operation.catalog_entry.alias if observed_record is not None
                    and inspection.operation.catalog_entry is not None else None),
                operation_status=observed_record.status.value if observed_record is not None else None,
            ) from None


def _operation_summary_card(published, *, operation_ref=None):
    record = published.operation
    return OperationSummaryCard(operation_alias=published.catalog_entry.alias,
        operation_ref=operation_ref, sequence=published.catalog_entry.ordinal,
        operation_kind=record.operation_kind.value, tool_name=record.tool_name,
        outcome=record.status.value, initiator=record.initiator.value,
        intent_summary=record.intent_summary or "", started_at=record.started_at,
        finished_at=record.finished_at, error_code=record.error_code)


def _product_report_tables(package):
    """Project only actual immutable report payload metadata, never expected names."""
    tables = []
    for member in package.members:
        if member.member_kind is not PackageMemberKind.PAYLOAD:
            continue
        if member.media_type != "application/vnd.apache.parquet" or member.row_count is None:
            raise ValueError("Product report payload metadata is incomplete or unsupported")
        tables.append(ProductReportTable(payload_name=member.logical_payload_name,
            row_count=member.row_count, logical_key_fields=member.logical_key_fields))
    if not tables or len({item.payload_name for item in tables}) != len(tables):
        raise ValueError("Product report payload metadata is missing or ambiguous")
    return tuple(tables)


def _operation_selection_card(
    operation: PublishedCriticalOperation,
    result_package: PublishedDataPackage | None,
    *,
    inspection_result: GeometryInspectionOperationResult | None = None,
    authoring_result: dict | None = None,
    operation_ref: str | None = None, same_run: bool = True,
    committed_ifc_revision: ExactIfcRevision | None = None,
):
    record = operation.operation
    if (
        record.status
        not in {
            CriticalOperationStatus.SUCCEEDED,
            CriticalOperationStatus.FAILED,
        }
        or record.finished_at is None
    ):
        raise ValueError("only terminal business Operations can be projected")
    common = {
        "operation_alias": operation.catalog_entry.alias if same_run else None,
        "operation_ref": operation_ref,
        "sequence": operation.catalog_entry.ordinal,
        "initiator": record.initiator.value,
        "intent_summary": record.intent_summary or "",
        "started_at": record.started_at,
        "finished_at": record.finished_at,
    }
    if isinstance(record.detail, DomainIfcProductsApplyOperationDetail):
        summary = record.detail.result
        if (result_package is None) != (summary is None):
            raise ValueError("Apply diagnostic report and summary must be present together")
        if result_package is not None and record.result_package_id != result_package.package.package_id:
            raise ValueError("Apply report Package differs from its exact Operation result")
        primary = ProductApplyReportPrimaryResult(report_package=_primary_result(result_package),
            report_tables=_product_report_tables(result_package), summary=summary) if result_package else None
        if record.status is CriticalOperationStatus.SUCCEEDED:
            if primary is None or summary.outcome != "committed" or record.result_ifc_revision_id is None:
                raise ValueError("successful Apply lost its exact committed revision or report")
            if (committed_ifc_revision is None
                    or committed_ifc_revision.revision_id != record.result_ifc_revision_id
                    or committed_ifc_revision.scope.run_id != record.run_id):
                raise ValueError("successful Apply requires its original committed IFC metadata")
            return SucceededIfcProductsApplyOperationCard(**common,
                primary_result=CommittedProductApplyPrimaryResult(**primary.model_dump(),
                    committed_revision=ProductApplyCommittedRevision(
                        revision_no=committed_ifc_revision.revision_no,
                        size_bytes=committed_ifc_revision.artifact.size_bytes)))
        if committed_ifc_revision is not None:
            raise ValueError("failed Apply cannot carry committed IFC metadata")
        if record.result_ifc_revision_id is not None or record.error_code is None or (summary is not None and summary.outcome == "committed"):
            raise ValueError("failed Apply cannot project committed products")
        return FailedIfcProductsApplyOperationCard(**common, error_code=record.error_code, primary_result=primary)
    if record.status is CriticalOperationStatus.SUCCEEDED:
        if isinstance(record.detail, DomainAuthoringDraftWriteOperationDetail):
            if (authoring_result is None or authoring_result.get("root_kind") != record.detail.root_kind
                or authoring_result.get("edit_token") != record.detail.result_generation):
                raise ValueError("draft Operation requires its original saved edit token")
            card_type = (SucceededGeometryResourceWriteOperationCard if record.detail.root_kind == "geometry"
                         else SucceededProductFamilyWriteOperationCard)
            return card_type(**common, operation_kind=record.operation_kind.value, tool_name=record.tool_name,
                outcome="succeeded", primary_result=AuthoringDraftPrimaryResult.model_validate(authoring_result))
        if isinstance(record.detail, DomainAuthoringPublishOperationDetail):
            if authoring_result is None:
                raise ValueError("publication Operation requires its exact result references")
            return SucceededAuthoringPublishOperationCard(**common,
                primary_result=AuthoringPublishPrimaryResult.model_validate({key: value for key, value in authoring_result.items()
                    if key != "preview_examples"}))
        if record.operation_kind is CriticalOperationKind.GEOMETRY_INSPECTION:
            if inspection_result is not None and inspection_result.result_kind == "case_report":
                if not isinstance(record.detail, DomainGeometryInspectionOperationDetail):
                    raise ValueError("Preview report operation lost its source detail")
                return SucceededGeometryInspectionOperationCard(
                    **common, operation_kind="geometry_inspection", tool_name=record.tool_name,
                    outcome="succeeded", primary_result=PreviewCaseReportPrimaryResult(
                        source_kind=inspection_result.source_kind, source_ref=inspection_result.source_version_ref,
                        case_count=inspection_result.case_count, checks_passed=inspection_result.checks_passed,
                        summary=inspection_result.summary,
                        cases=inspection_result.case_index,
                    ),
                )
            if (
                result_package is not None
                or record.result_inspection_artifact is None
                or not isinstance(record.detail, DomainGeometryInspectionOperationDetail)
                or inspection_result is None or inspection_result.inspection_ref is None
                or inspection_result.source_kind != record.detail.source_kind
                or inspection_result.source_version_ref != record.detail.source_version_ref
            ):
                raise ValueError("retained Preview Operation result is inconsistent")
            return SucceededGeometryInspectionOperationCard(
                **common, operation_kind="geometry_inspection", tool_name=record.tool_name,
                outcome="succeeded", primary_result=GeometryInspectionPrimaryResult(
                    inspection_ref=inspection_result.inspection_ref,
                    source_kind=inspection_result.source_kind,
                    source_version_ref=inspection_result.source_version_ref,
                    summary=inspection_result.summary,
                ),
            )
        if record.operation_kind is CriticalOperationKind.PRODUCT_FAMILY_WRITE:
            if (
                result_package is not None
                or record.result_package_id is not None
                or record.result_ifc_revision_id is not None
                or record.result_geometry_resource_version_id is not None
                or record.result_product_family_version_id is None
                or not isinstance(record.detail, DomainProductFamilyWriteOperationDetail)
                or record.detail.mode != "system_copy"
            ):
                raise ValueError("successful ProductFamily Operation result is inconsistent")
            return SucceededProductFamilyWriteOperationCard(
                **common,
                operation_kind="product_family_write",
                tool_name="ifc_product_family_write",
                outcome="succeeded",
                primary_result=ProductFamilyWritePrimaryResult(
                    result="product_family_version_created"
                ),
            )
        if record.operation_kind is CriticalOperationKind.IFC_MODEL_WRITE:
            if (
                result_package is not None
                or record.result_package_id is not None
                or record.result_ifc_revision_id is None
            ):
                raise ValueError("successful IFC Operation result is inconsistent")
            if isinstance(record.detail, DomainIfcTrustedBaselineLoadOperationDetail):
                if (record.detail.result != "baseline_loaded"
                        or committed_ifc_revision is None
                        or committed_ifc_revision.revision_id != record.result_ifc_revision_id
                        or committed_ifc_revision.scope.run_id != record.run_id):
                    raise ValueError("trusted baseline requires its original committed IFC metadata")
                return SucceededIfcTrustedBaselineLoadOperationCard(
                    **common, operation_kind="ifc_model_write", tool_name="ifc_trusted_baseline_load",
                    outcome="succeeded", primary_result=IfcTrustedBaselineLoadPrimaryResult(
                        result="baseline_loaded", committed_revision=ProductApplyCommittedRevision(
                            revision_no=committed_ifc_revision.revision_no,
                            size_bytes=committed_ifc_revision.artifact.size_bytes)),
                )
            if isinstance(record.detail, DomainIfcModelInitializeOperationDetail):
                if record.detail.result != "model_initialized":
                    raise ValueError("successful IFC initialization result is inconsistent")
                return SucceededIfcModelWriteOperationCard(
                    **common,
                    operation_kind="ifc_model_write",
                    tool_name="ifc_model_initialize",
                    outcome="succeeded",
                    primary_result=IfcModelPrimaryResult(
                        ifc_schema=record.detail.ifc_schema,
                        project_global_id=record.detail.project_global_id,
                        project_name=record.detail.project_name,
                        result="model_initialized",
                    ),
                )
            if (
                isinstance(record.detail, DomainIfcGeometryBasisConfigureOperationDetail)
                and record.detail.result == "geometry_basis_configured"
            ):
                return SucceededIfcGeometryBasisConfigureOperationCard(
                    **common,
                    operation_kind="ifc_model_write",
                    tool_name="ifc_model_configure_geometry_basis",
                    outcome="succeeded",
                    primary_result=IfcGeometryBasisConfigurePrimaryResult(
                        result="geometry_basis_configured"
                    ),
                )
            if (
                isinstance(record.detail, DomainIfcProjectUpdateOperationDetail)
                and record.detail.result is not None
            ):
                return SucceededIfcProjectUpdateOperationCard(
                    **common,
                    operation_kind="ifc_model_write",
                    tool_name="ifc_project_update",
                    outcome="succeeded",
                    primary_result=IfcProjectUpdatePrimaryResult(result="project_updated"),
                )
            if (
                isinstance(record.detail, DomainIfcSpatialStructureApplyOperationDetail)
                and record.detail.result is not None
            ):
                return SucceededIfcSpatialStructureApplyOperationCard(
                    **common,
                    operation_kind="ifc_model_write",
                    tool_name="ifc_spatial_structure_apply",
                    outcome="succeeded",
                    primary_result=IfcSpatialStructureApplyPrimaryResult(
                        result="spatial_structure_applied",
                        applied_count=record.detail.result.applied_count,
                    ),
                )
            raise ValueError("successful IFC Operation detail is inconsistent")
        if result_package is None or record.result_package_id != result_package.package.package_id:
            raise ValueError("successful Operation result Package is inconsistent")
        primary_result = _primary_result(result_package)
        if record.operation_kind is CriticalOperationKind.SOURCE_INGESTION:
            return SucceededSourceIngestionOperationCard(
                **common,
                operation_kind="source_ingestion",
                tool_name="cad_import_source",
                outcome="succeeded",
                primary_result=primary_result,
            )
        if record.operation_kind is CriticalOperationKind.DATA_TRANSFORM:
            return SucceededDataTransformOperationCard(
                **common,
                operation_kind="data_transform",
                tool_name=record.tool_name,
                outcome="succeeded",
                primary_result=primary_result,
            )
        raise ValueError("unsupported successful Operation kind")

    if result_package is not None or record.error_code is None:
        raise ValueError("failed Operation terminal metadata is inconsistent")
    if record.operation_kind is CriticalOperationKind.GEOMETRY_INSPECTION:
        if (
            not isinstance(record.detail, DomainGeometryInspectionOperationDetail)
            or (record.result_inspection_artifact is not None and record.detail.result_kind != "case_report")
        ):
            raise ValueError("failed Preview can retain only its exact partial case report")
        return FailedGeometryInspectionOperationCard(
            **common, operation_kind="geometry_inspection", tool_name=record.tool_name,
            outcome="failed", error_code=record.error_code,
            primary_result=PreviewCaseReportPrimaryResult(
                source_kind=inspection_result.source_kind, source_ref=inspection_result.source_version_ref,
                case_count=inspection_result.case_count, checks_passed=inspection_result.checks_passed,
                summary=inspection_result.summary, cases=inspection_result.case_index,
            ) if inspection_result and inspection_result.case_count is not None else None,
        )
    if record.operation_kind is CriticalOperationKind.PRODUCT_FAMILY_WRITE:
        if record.result_product_family_version_id is not None or not isinstance(
            record.detail, DomainProductFamilyWriteOperationDetail
        ):
            raise ValueError("failed ProductFamily Operation result is inconsistent")
        return FailedProductFamilyWriteOperationCard(
            **common,
            operation_kind="product_family_write",
            tool_name="ifc_product_family_write",
            outcome="failed",
            error_code=record.error_code,
        )
    if record.operation_kind is CriticalOperationKind.IFC_MODEL_WRITE:
        if record.result_package_id is not None or record.result_ifc_revision_id is not None:
            raise ValueError("failed IFC Operation cannot contain a result")
        if isinstance(record.detail, DomainIfcTrustedBaselineLoadOperationDetail):
            return FailedIfcTrustedBaselineLoadOperationCard(
                **common, operation_kind="ifc_model_write", tool_name="ifc_trusted_baseline_load",
                outcome="failed", error_code=record.error_code,
            )
        if isinstance(record.detail, DomainIfcGeometryBasisConfigureOperationDetail):
            return FailedIfcGeometryBasisConfigureOperationCard(
                **common,
                operation_kind="ifc_model_write",
                tool_name="ifc_model_configure_geometry_basis",
                outcome="failed",
                error_code=record.error_code,
            )
        if isinstance(record.detail, DomainIfcProjectUpdateOperationDetail):
            return FailedIfcProjectUpdateOperationCard(
                **common,
                operation_kind="ifc_model_write",
                tool_name="ifc_project_update",
                outcome="failed",
                error_code=record.error_code,
            )
        if isinstance(record.detail, DomainIfcSpatialStructureApplyOperationDetail):
            return FailedIfcSpatialStructureApplyOperationCard(
                **common,
                operation_kind="ifc_model_write",
                tool_name="ifc_spatial_structure_apply",
                outcome="failed",
                error_code=record.error_code,
            )
        if not isinstance(record.detail, DomainIfcModelInitializeOperationDetail):
            raise ValueError("failed IFC Operation detail is inconsistent")
        return FailedIfcModelWriteOperationCard(
            **common,
            operation_kind="ifc_model_write",
            tool_name="ifc_model_initialize",
            outcome="failed",
            error_code=record.error_code,
        )
    if record.operation_kind is CriticalOperationKind.AUTHORING_PUBLISH:
        return FailedAuthoringPublishOperationCard(**common,
            root_kind=record.detail.root_kind, error_code=record.error_code)
    if record.operation_kind is CriticalOperationKind.SOURCE_INGESTION:
        return FailedSourceIngestionOperationCard(
            **common,
            operation_kind="source_ingestion",
            tool_name="cad_import_source",
            outcome="failed",
            error_code=record.error_code,
        )
    if record.operation_kind is CriticalOperationKind.DATA_TRANSFORM:
        return FailedDataTransformOperationCard(
            **common,
            operation_kind="data_transform",
            tool_name=record.tool_name,
            outcome="failed",
            error_code=record.error_code,
        )
    raise ValueError("unsupported failed Operation kind")


def _operation_get_result(
    inspection: OperationInspection,
    *,
    inspection_result: GeometryInspectionOperationResult | None = None,
    authoring_result: dict | None = None,
    case_key: str | None = None, after_case_key: str | None = None, page_size: int = 1,
    operation_ref: str | None = None, same_run: bool = True,
    family_version_ref: str | None = None,
    code_locator: str | None = None,
) -> OperationGetResult:
    published = inspection.operation
    record = published.operation
    if isinstance(record.detail, DomainIfcProductsEditOperationDetail):
        return project_ifc_products_edit_operation(inspection, operation_ref=operation_ref, same_run=same_run)
    card = _operation_selection_card(
        published, inspection.result_package, inspection_result=inspection_result,
        authoring_result=authoring_result,
        operation_ref=operation_ref, same_run=same_run,
        committed_ifc_revision=(inspection.committed_ifc_revision
            if isinstance(record.detail, (DomainIfcProductsApplyOperationDetail,
                DomainIfcTrustedBaselineLoadOperationDetail)) else None),
    )
    detail = record.detail
    if isinstance(detail, DomainAuthoringDraftWriteOperationDetail):
        projected = AuthoringDraftOperationDetail.model_validate(authoring_result)
        return _bounded_operation_get_result(SucceededOperationGetResult(operation=card, detail=projected))
    if isinstance(detail, DomainAuthoringPublishOperationDetail):
        if record.status is CriticalOperationStatus.SUCCEEDED:
            projected = AuthoringPublishOperationDetail.model_validate(authoring_result)
            return _bounded_operation_get_result(SucceededOperationGetResult(operation=card, detail=projected))
        projected = FailedAuthoringPublishOperationDetail(root_kind=detail.root_kind)
        return _bounded_operation_get_result(FailedOperationGetResult(operation=card, detail=projected,
            error=OperationErrorDetail(code=record.error_code, phase=record.error_phase.value,
                message=record.error_message, retryable=False)))
    if isinstance(detail, DomainGeometryInspectionOperationDetail):
        if (
            inspection_result is None or inspection_result.request_json is None
            or inspection_result.source_kind != detail.source_kind
            or inspection_result.source_version_ref != detail.source_version_ref
        ):
            raise ValueError("retained Preview original request projection is incomplete")
        projected_detail = GeometryInspectionOperationDetail(
            source_kind=inspection_result.source_kind,
            source_version_ref=inspection_result.source_version_ref,
            request_json=inspection_result.request_json,
            inspection_ref=inspection_result.inspection_ref,
        )
    elif isinstance(detail, DomainIfcProductsApplyOperationDetail):
        projected_detail = _product_apply_detail(inspection, family_version_ref=family_version_ref)
    elif isinstance(detail, DomainSourceIngestionOperationDetail):
        projected_detail = SourceIngestionOperationDetail(
            detail_kind="source_ingestion",
            source=SourceIngestionSourceDetail(
                filename=detail.filename,
                media_type=detail.media_type,
                size_bytes=detail.size_bytes,
            ),
            code_available=False,
        )
    elif isinstance(detail, DomainDataTransformOperationDetail):
        from CAD2BIM_backend.application.data_runtime_service import transform_metadata_page
        projected_detail = DataTransformOperationDetail(
            detail_kind="data_transform",
            ifc_source=({"revision_no": detail.ifc_source.revision_no} if detail.ifc_source is not None else None),
            engine=detail.engine,
            inputs=transform_metadata_page(detail, "inputs", inspection.input_packages),
            parameters=transform_metadata_page(detail, "parameters", inspection.input_packages),
            inline_tables=transform_metadata_page(detail, "inline_tables", inspection.input_packages),
            outputs=transform_metadata_page(detail, "outputs", inspection.input_packages),
            output_package_name=detail.output_package_name,
            code_available=code_locator is not None,
            code_locator=code_locator,
            repair_parent_operation_alias=(
                getattr(inspection, "repair_parent_operation").catalog_entry.alias
                if getattr(inspection, "repair_parent_operation", None) is not None
                else None
            ),
        )
    elif isinstance(detail, DomainCadMeasurementOperationDetail):
        source_packages = {
            inspection.input_packages[binding.input_name].catalog_entry.alias
            for binding in detail.inputs
            if binding.input_name in inspection.input_packages
        }
        if len(source_packages) != 1:
            raise ValueError("CAD measurement source Package alias could not be resolved")
        projected_detail = CadMeasurementOperationDetail(
            detail_kind="cad_measurement",
            engine="cad_measurement",
            source_package_alias=source_packages.pop(),
            source_unit_to_m=detail.source_unit_to_m,
            path_layers=detail.path_layers,
            point_scopes=detail.point_scopes,
            tolerance_spec=detail.tolerance_spec,
            outputs=tuple(
                ParquetOperationOutputDeclaration(
                    payload_name=item.payload_name,
                    format="parquet",
                    description=item.description,
                    logical_key_fields=item.logical_key_fields,
                )
                for item in detail.outputs
            ),
            output_package_name=detail.output_package_name,
            code_available=True,
        )
    elif isinstance(detail, DomainIfcTrustedBaselineLoadOperationDetail):
        projected_detail = IfcTrustedBaselineLoadOperationDetail(
            detail_kind="ifc_trusted_baseline_load", result=detail.result,
        )
    elif isinstance(detail, DomainIfcModelInitializeOperationDetail):
        projected_detail = IfcModelInitializeOperationDetail(
            detail_kind="ifc_model_initialize",
            ifc_schema=detail.ifc_schema,
            project_name=detail.project_name,
            project_global_id=detail.project_global_id,
            result=detail.result,
        )
    elif isinstance(detail, DomainIfcGeometryBasisConfigureOperationDetail):
        projected_detail = IfcGeometryBasisConfigureOperationDetail(
            detail_kind="ifc_model_configure_geometry_basis",
            request=IfcGeometryBasisConfigureRequest.model_validate(
                detail.request.model_dump(mode="json")
            ),
            result=detail.result,
        )
    elif isinstance(detail, DomainIfcProjectUpdateOperationDetail):
        projected_detail = IfcProjectUpdateOperationDetail(
            detail_kind="ifc_project_update",
            request=detail.request,
            result="project_updated" if detail.result is not None else None,
        )
    elif isinstance(detail, DomainIfcSpatialStructureApplyOperationDetail):
        projected_detail = IfcSpatialStructureApplyOperationDetail(
            detail_kind="ifc_spatial_structure_apply",
            request=detail.request,
            result="spatial_structure_applied" if detail.result is not None else None,
            applied_count=len(detail.result.applied) if detail.result is not None else None,
        )
    elif isinstance(detail, DomainProductFamilyWriteOperationDetail) and detail.mode == "system_copy":
        if record.code_artifact is None:
            raise ValueError("ProductFamily Operation requires exact source")
        draft = detail.version_draft
        contract = draft.family_contract
        projected_detail = ProductFamilyWriteOperationDetail(
            detail_kind="product_family_write",
            mode=detail.mode,
            display_name=draft.display_name,
            purpose=draft.purpose,
            product_structure_kind=contract.product_structure_kind.value,
            component_count=len(contract.components),
            geometry_fragment_count=len(contract.geometry_fragments),
            interface_count=len(contract.interfaces),
            geometry_binding_count=len(draft.geometry_bindings),
            program_entry=draft.program_entry,
            code_available=True,
            source_bundle_sha256=record.code_artifact.sha256,
        )
    else:
        raise ValueError("Operation detail is missing or unsupported")

    if record.status is CriticalOperationStatus.SUCCEEDED:
        result = SucceededOperationGetResult(operation=card, detail=projected_detail)
        return (_bounded_product_report_result(result)
            if isinstance(detail, DomainIfcProductsApplyOperationDetail) else _bounded_operation_get_result(result))
    if record.error_code is None or record.error_message is None or record.error_phase is None:
        raise ValueError("failed Operation error detail is incomplete")
    result = FailedOperationGetResult(
        operation=card,
        detail=projected_detail,
        error=OperationErrorDetail(
            code=record.error_code,
            phase=record.error_phase.value,
            message=record.error_message,
            retryable=False,
        ),
    )
    return (_bounded_product_report_result(result)
        if isinstance(detail, DomainIfcProductsApplyOperationDetail) else _bounded_operation_get_result(result))


def _bounded_operation_get_result(result: OperationGetResult) -> OperationGetResult:
    """Enforce the public response budget after projecting the original fact."""

    if len(result.model_dump_json().encode("utf-8")) > _OPERATION_GET_RESPONSE_BYTES:
        raise _tool_error(DirectApplicationError(
            "OPERATION_GET_RESPONSE_TOO_LARGE",
            "The complete original Operation detail exceeds the 64 KiB operation_get "
            f"response budget. The original Operation remains {result.operation.outcome}; "
            "use operation_list for its bounded selection card.",
            phase="output_validation",
            operation_alias=result.operation.operation_alias,
        ))
    return result


def _bounded_operation_catalog_result(
    cards: tuple,
    *,
    complete: bool,
    next_before_sequence: int | None,
) -> OperationListResult:
    return bounded_keyset_directory(
        cards,
        complete=complete,
        next_before_sequence=next_before_sequence,
        sequence_of=lambda card: card.sequence,
        build_result=lambda selected, page_complete, continuation: OperationListResult(
            operations=selected,
            complete=page_complete,
            next_before_sequence=continuation,
        ),
        item_label="Operation",
    )


def _product_apply_detail(inspection, *, family_version_ref):
    detail = inspection.operation.operation.detail
    if not isinstance(detail, DomainIfcProductsApplyOperationDetail):
        raise ValueError("The selected Operation is not a current product Apply")
    binding = detail.input_binding
    package = inspection.input_packages.get(binding.payload_name)
    if not family_version_ref or package is None or package.package.package_id != binding.package_id:
        raise ValueError("Apply exact Family or input Package could not be authorized")
    return IfcProductsApplyOperationDetail(family_version_ref=family_version_ref,
        input_binding=ProductApplyInputBinding(package_alias=package.catalog_entry.alias,
            payload_name=binding.payload_name, logical_key_fields=binding.logical_key_fields),
        diagnostics=inspection.diagnostics)


def project_ifc_apply_operation(inspection, *, family_version_ref, operation_ref, same_run=True):
    """One public Apply projection shared by tool results and original-operation reads."""
    from CAD2BIM_mcp.direct.ifc.products.contracts import IfcProductsApplyResult
    from CAD2BIM_mcp.direct.operation.contracts import UnsettledIfcProductsApplyResult, UnsettledIfcProductsApplyCard

    record = inspection.operation.operation
    if not isinstance(record.detail, DomainIfcProductsApplyOperationDetail):
        raise ValueError("The original Operation is not a product Apply")
    if record.status in {CriticalOperationStatus.PREPARED, CriticalOperationStatus.INDETERMINATE}:
        result = UnsettledIfcProductsApplyResult(
            operation=UnsettledIfcProductsApplyCard(operation_ref=operation_ref,
                outcome=record.status.value, started_at=record.started_at,
                finished_at=record.finished_at, error_code=record.error_code),
            detail=_product_apply_detail(inspection, family_version_ref=family_version_ref))
    else:
        result = _operation_get_result(inspection, family_version_ref=family_version_ref,
            operation_ref=operation_ref, same_run=same_run)
    return TypeAdapter(IfcProductsApplyResult).validate_python(result.model_dump())


def project_ifc_products_edit_operation(inspection, *, operation_ref, same_run=True):
    """Project one saved edit fact set for first response, get and recovery alike."""
    from CAD2BIM_mcp.direct.ifc.products.contracts import (
        IfcProductsEditResult, SucceededProductsEditResult, FailedProductsEditResult,
    )
    from CAD2BIM_mcp.direct.operation.contracts import (
        ProductsEditDetail, ProductApplyInputBinding, ProductsEditReportPrimaryResult,
        CommittedProductsEditPrimaryResult, SucceededProductsEditCard, FailedProductsEditCard,
        UnsettledProductsEditCard, UnsettledProductsEditResult,
    )
    record = inspection.operation.operation
    detail = record.detail
    if record.tool_name != "ifc_products_edit" or not isinstance(detail, DomainIfcProductsEditOperationDetail):
        raise ValueError("The original Operation is not a product edit")
    if not operation_ref:
        raise ValueError("The original edit Operation requires its authorized recovery reference")
    binding = detail.input_binding
    package = inspection.input_packages.get(binding.payload_name)
    if package is None or package.package.package_id != binding.package_id:
        raise ValueError("The original edit input Package could not be authorized")
    projected_detail = ProductsEditDetail(
        input_binding=ProductApplyInputBinding(package_alias=package.catalog_entry.alias,
            payload_name=binding.payload_name, logical_key_fields=binding.logical_key_fields),
        base_revision_no=inspection.base_revision_no, diagnostics=inspection.diagnostics)
    if record.status in {CriticalOperationStatus.PREPARED, CriticalOperationStatus.INDETERMINATE}:
        if inspection.result_package is not None or detail.result is not None or record.result_ifc_revision_id is not None:
            raise ValueError("An unsettled edit cannot carry an invented terminal report or summary")
        result = UnsettledProductsEditResult(operation=UnsettledProductsEditCard(
            operation_ref=operation_ref, outcome=record.status.value, started_at=record.started_at,
            finished_at=record.finished_at, error_code=record.error_code), detail=projected_detail)
    else:
        summary = detail.result
        report = inspection.result_package
        if (report is None) != (summary is None):
            raise ValueError("The original edit report and summary must be available together")
        if report is not None and record.result_package_id != report.package.package_id:
            raise ValueError("The edit report differs from the original Operation result")
        primary = (ProductsEditReportPrimaryResult(report_package=_primary_result(report),
            report_tables=_product_report_tables(report), summary=summary)
            if report is not None else None)
        catalog = inspection.operation.catalog_entry
        common = dict(operation_ref=operation_ref,
            operation_alias=catalog.alias if same_run else None, sequence=catalog.ordinal,
            initiator=record.initiator.value, intent_summary=record.intent_summary,
            started_at=record.started_at, finished_at=record.finished_at)
        exact = inspection.committed_ifc_revision
        if record.status is CriticalOperationStatus.SUCCEEDED:
            if (primary is None or exact is None or exact.revision_id != record.result_ifc_revision_id
                    or exact.scope.run_id != record.run_id):
                raise ValueError("A successful edit requires its original report and exact committed revision")
            result = SucceededProductsEditResult(operation=SucceededProductsEditCard(**common,
                primary_result=CommittedProductsEditPrimaryResult(**primary.model_dump(),
                    committed_revision=ProductApplyCommittedRevision(revision_no=exact.revision_no,
                        size_bytes=exact.artifact.size_bytes))), detail=projected_detail)
        elif record.status is CriticalOperationStatus.FAILED:
            if exact is not None or record.result_ifc_revision_id is not None:
                raise ValueError("A failed edit cannot project a commit")
            result = FailedProductsEditResult(operation=FailedProductsEditCard(**common,
                error_code=record.error_code, primary_result=primary), detail=projected_detail,
                error=OperationErrorDetail(code=record.error_code, phase=record.error_phase.value,
                    message=record.error_message, retryable=False))
        else:
            raise ValueError("Unsupported product edit Operation state")
    return _bounded_product_report_result(TypeAdapter(IfcProductsEditResult).validate_python(result.model_dump()))


def _bounded_product_report_result(result):
    # The saved checks remain authoritative. Fit actual diagnostics to the same
    # existing response budget, keeping errors ahead of warnings and marking any
    # omitted saved facts explicitly. No fixed item-count quota is introduced.
    diagnostics = result.detail.diagnostics
    if diagnostics is not None and len(result.model_dump_json().encode("utf-8")) > _OPERATION_GET_RESPONSE_BYTES:
        ordered = tuple(sorted(diagnostics.items, key=lambda item: item.severity != "error"))
        low, high, best = 0, len(ordered), None
        while low <= high:
            count = (low + high) // 2
            candidate = result.model_copy(update={"detail": result.detail.model_copy(update={
                "diagnostics": diagnostics.model_copy(update={"items": ordered[:count], "complete": False})})})
            if len(candidate.model_dump_json().encode("utf-8")) <= _OPERATION_GET_RESPONSE_BYTES:
                best, low = candidate, count + 1
            else:
                high = count - 1
        if best is not None:
            result = best
    return _bounded_operation_get_result(result)


async def list_ifc_products_recovery(scope, *, data_runtime, references, tool_name, cursor=None, page_size=20):
    """Page the original Operation records; no Catalog entry or write is created."""
    from CAD2BIM_mcp.direct.operation.contracts import IfcProductsRecoveryCard, IfcProductsRecoveryListResult

    before_created_at, before_operation_id = (references.parse_ifc_products_recovery_cursor(scope, cursor, tool_name=tool_name)
        if cursor is not None else (None, None))
    records = await data_runtime.list_ifc_products_recovery_operations(scope, tool_name=tool_name,
        before_created_at=before_created_at, before_operation_id=before_operation_id, limit=page_size + 1)
    selected = records[:page_size]
    complete = len(records) <= page_size
    next_cursor = (None if complete else references.ifc_products_recovery_cursor(scope, tool_name=tool_name,
        created_at=selected[-1].created_at, operation_id=selected[-1].operation.operation_id))
    return IfcProductsRecoveryListResult(operations=tuple(IfcProductsRecoveryCard(
        tool_name=item.operation.tool_name,
        operation_ref=references.operation_reference(scope.identity,
            origin_run_id=item.operation.run_id, operation_id=item.operation.operation_id),
        operation_alias=item.catalog_entry.alias if item.catalog_entry is not None else None,
        outcome=item.operation.status.value, started_at=item.operation.started_at,
        finished_at=item.operation.finished_at, error_code=item.operation.error_code) for item in selected),
        complete=complete, next_cursor=next_cursor)
