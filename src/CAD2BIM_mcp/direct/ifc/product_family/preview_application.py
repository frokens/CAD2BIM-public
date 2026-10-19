"""Direct projection over the entry-neutral Product Realization preview services."""

from __future__ import annotations

from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError
from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringApplicationError

from CAD2BIM_mcp.direct.ifc.preview_cases import parse_cases, parse_render, project_case_batch, external_target_comparison
from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import DirectIfcProductRealizationCasesInput

from CAD2BIM_mcp.direct.geometry_preview_contracts import (
    IfcGeometryPreviewReferenceResult,
    project_preview_presentation,
)

from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Protocol

from pydantic import ValidationError

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogError
from CAD2BIM_backend.domain.product_realization import (
    AmbiguousProductRealizationProposal,
    InfeasibleProductRealizationProposal,
    ProductRealizationInvocation,
    SolvedProductRealizationProposal,
)
from CAD2BIM_backend.domain.product_realization_preview import (
    ProductRealizationPreviewError,
    ProductRealizationPreviewErrorCode,
)
from CAD2BIM_mcp.direct.application_support import _direct_error
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.product_family.preview_contracts import (
    DirectIfcProductRealizationPreviewRequest,
    DirectIfcProductRealizationStandardModelInput,
    DirectIfcProductRealizationSelfGeometryInput,
    IfcProductRealizationAmbiguousResult,
    IfcProductRealizationInfeasibleResult,
    IfcProductRealizationFragmentPlacementResult,
    IfcProductRealizationInstanceReceiptResult,
    IfcProductRealizationPreviewResult,
    IfcProductRealizationSolvedResult,
    IfcProductRealizationRetainedNoImageSolvedResult,
    product_realization_parameter_values,
)

if TYPE_CHECKING:
    from CAD2BIM_backend.application.product_family_catalog_service import (
        ProductFamilyCatalogService,
    )
    from CAD2BIM_backend.application.product_realization_preview_service import (
        ProductRealizationPreviewService,
    )
    from CAD2BIM_backend.domain.invocation import InvocationScope


from CAD2BIM_backend.domain.runtime_execution import PRODUCT_PREVIEW_EXECUTION, RUNTIME_RESPONSE_MARGIN_SECONDS

_PREVIEW_TIMEOUT_SECONDS = PRODUCT_PREVIEW_EXECUTION.total_seconds + RUNTIME_RESPONSE_MARGIN_SECONDS


@dataclass(frozen=True, slots=True)
class IfcProductRealizationPreviewAuthorization:
    """Provider result plus exact PNG bytes only when the result is SOLVED."""

    result: IfcProductRealizationPreviewResult
    content: bytes | None = None
    title: str | None = None
    images: tuple[bytes, ...] = ()


class ProductRealizationPreviewProviderApplication(Protocol):
    async def preview_ifc_product_realization(
        self,
        request: DirectIfcProductRealizationPreviewRequest,
        *, request_identity: str | None = None,
    ) -> IfcProductRealizationPreviewAuthorization: ...


class IfcProductRealizationPreviewApplicationMixin:
    """Resolve the current invocation and delegate one exact read-only solve."""

    async def preview_ifc_product_realization(
        self,
        request: DirectIfcProductRealizationPreviewRequest,
        *, request_identity: str | None = None,
    ) -> IfcProductRealizationPreviewAuthorization:
        deadline = monotonic() + PRODUCT_PREVIEW_EXECUTION.total_seconds

        async def resolve_and_preview(scope: InvocationScope) -> IfcProductRealizationPreviewAuthorization:
            service = self._require_product_realization_preview_service()
            if isinstance(request, DirectIfcProductRealizationCasesInput):
                cases = await self._require_geometry_inspection_service().resolve_cases(
                    scope, parse_cases(request.cases_json, case_kind="family"), case_kind="family",
                )
                if request.source.source_kind == "draft":
                    prepared = await self._require_product_family_authoring_service().prepare_draft_preview(
                        scope, draft_ref=request.source.draft_ref, invocations=tuple(item.invocation for item in cases),
                    )
                else:
                    prepared = await service.prepare_published_cases(scope, request.source.family_version_alias, cases)
                outcome = await service.preview_cases(
                    scope, **prepared, cases=cases, nominal_scenario_key=request.nominal_scenario_key,
                    retain_for_inspection=request.retain, render=parse_render(request.render_json),
                    request_identity=request_identity,
                    reference_geometries=tuple(item.to_domain() for item in request.reference_geometries),
                )
                return IfcProductRealizationPreviewAuthorization(
                    result=IfcProductRealizationPreviewResult(result=project_case_batch(outcome)),
                    images=tuple(item.payload for _, item in outcome.images),
                )
            if isinstance(request, DirectIfcProductRealizationSelfGeometryInput):
                invocation = ProductRealizationInvocation(
                    realization_kind="self_geometry", public_parameters=request.public_parameters,
                    product_origin=request.product_origin, length_unit=request.length_unit,
                    external_constraints=request.external_constraints,
                )
            elif isinstance(request, DirectIfcProductRealizationStandardModelInput):
                invocation = ProductRealizationInvocation(
                    realization_kind="standard_model",
                    public_parameters=request.public_parameters,
                    product_origin=request.product_origin,
                    length_unit=request.length_unit,
                )
            else:  # pragma: no cover - closed Provider union
                raise DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "The Product Realization attachment branch is invalid",
                    phase="preflight",
                )

            common = dict(reference_geometries=tuple(item.to_domain() for item in request.reference_geometries),
                          render_profile=request.render_profile, request_identity=request_identity,
                          retain_for_inspection=request.retain)
            if request.source.source_kind == "draft":
                prepared = await self._require_product_family_authoring_service().prepare_draft_preview(
                    scope, draft_ref=request.source.draft_ref, invocations=(invocation,),
                )
                requests = prepared.pop("requests")
                outcome = await service.preview_loaded(scope, **prepared, request=requests[0] if requests is not None else None, **common)
                source_ref = request.source.draft_ref
            else:
                family = await self._require_product_family_catalog_service().resolve_exact_version(
                    scope.identity, request.source.family_version_alias,
                )
                outcome = await service.preview(scope, family_version=family, invocation=invocation, deadline=deadline, **common)
                source_ref = request.source.family_version_alias
            return _project_preview_outcome(source_ref, request.attachment_kind, outcome)

        return await self._product_realization_preview_read(
            request.direct_session_alias,
            resolve_and_preview,
            timeout_seconds=_PREVIEW_TIMEOUT_SECONDS,
            input_source="cases_json" if isinstance(request, DirectIfcProductRealizationCasesInput) else "invocation_json",
        )

    async def _product_realization_preview_read(
        self,
        direct_session_alias: str,
        operation,
        *,
        timeout_seconds: float = _PREVIEW_TIMEOUT_SECONDS,
        input_source: str | None = None,
    ) -> IfcProductRealizationPreviewAuthorization:
        from CAD2BIM_backend.application.data_runtime_service import (
            DataRuntimeApplicationError,
        )
        from CAD2BIM_backend.application.geometry_inspection_service import GeometryInspectionApplicationError

        try:
            return await self._run_runtime_read(
                direct_session_alias,
                "ifc_product_realization_preview",
                operation,
                timeout_seconds=timeout_seconds,
                timeout_code="PRODUCT_REALIZATION_PREVIEW_TIMEOUT",
                timeout_message="The Product Realization preview read boundary expired",
            )
        except DirectApplicationError:
            raise
        except AuthoringDraftError as exc:
            raise DirectApplicationError(exc.code, str(exc), phase="preflight", retryable=False) from None
        except ProductFamilyAuthoringApplicationError as exc:
            raise DirectApplicationError(exc.code, str(exc), phase=exc.phase, retryable=exc.retryable,
                                         operation_alias=exc.operation_alias, diagnostic=exc.diagnostic) from None
        except GeometryInspectionApplicationError as exc:
            raise DirectApplicationError(
                exc.code, str(exc), phase=exc.phase, retryable=exc.retryable,
                operation_alias=exc.operation_alias,
            ) from None
        except DataRuntimeApplicationError as exc:
            raise _direct_error(exc, phase="preflight") from None
        except ProductFamilyCatalogError as exc:
            raise DirectApplicationError(
                exc.code,
                str(exc),
                phase="read",
                retryable=False,
            ) from None
        except ProductRealizationPreviewError as exc:
            message = str(exc)
            if exc.code is ProductRealizationPreviewErrorCode.INPUT_LOADING_FAILED and input_source is not None:
                message = f"provider_input_source={input_source}; {message}"
            raise DirectApplicationError(
                exc.code.value,
                message,
                phase=_preview_error_phase(exc.code),
                retryable=exc.retryable,
                diagnostic=exc.diagnostic,
            ) from None
        except (KeyError, TypeError, ValueError, ValidationError):
            raise DirectApplicationError(
                "PRODUCT_REALIZATION_PREVIEW_INTEGRITY_ERROR",
                "The exact Product Realization preview could not be safely projected",
                phase="output_validation",
                retryable=False,
            ) from None

    def _require_product_family_catalog_service(self) -> ProductFamilyCatalogService:
        service = getattr(self, "_product_family_catalog_service", None)
        if service is None:
            raise _service_unavailable("ProductFamily Catalog")
        return service

    def _require_product_realization_preview_service(self) -> ProductRealizationPreviewService:
        service = getattr(self, "_product_realization_preview_service", None)
        if service is None:
            raise _service_unavailable("Product Realization preview")
        return service


def _project_preview_outcome(
    source_ref: str,
    attachment_kind: str,
    outcome,
) -> IfcProductRealizationPreviewAuthorization:
    realization = outcome.realization
    proposal = realization.proposal
    common = {
        "operation_alias": outcome.operation_alias,
        "diagnostics": outcome.diagnostics,
        "source_ref": source_ref,
        "attachment_kind": attachment_kind,
        "normalized_public_parameters": product_realization_parameter_values(
            realization.normalized_public_parameters
        ),
    }
    if isinstance(proposal, SolvedProductRealizationProposal):
        preview = outcome.preview
        solved = dict(
            **common,
            inspection_ref=outcome.inspection_ref,
            status="SOLVED",
            internal_parameters_json=canonical_json(proposal.internal_parameters),
            components=proposal.components,
            fragments=tuple(
                IfcProductRealizationFragmentPlacementResult(
                    fragment_key=item.fragment_key,
                    binding_key=item.binding_key,
                    geometry_parameters_json=canonical_json(item.geometry_parameters),
                    local_transform=item.local_transform,
                    uniform_scale=(item.uniform_scale if item.uniform_scale != 1.0 else None),
                )
                for item in proposal.fragments
            ),
            instance_receipts=tuple(
                IfcProductRealizationInstanceReceiptResult(
                    fragment_key=item.fragment_key,
                    binding_key=item.binding_key,
                    normalized_parameters_json=canonical_json(item.normalized_parameters),
                    geometry_summary_json=canonical_json(item.geometry_summary),
                    uniform_scale=(item.uniform_scale if item.uniform_scale != 1.0 else None),
                )
                for item in realization.instance_receipts
            ),
            component_project_world_transforms=(realization.component_project_world_transforms),
            interface_project_world_frames=realization.interface_project_world_frames,
            frame_check_status=realization.frame_check_status,
            frame_diagnostics=realization.frame_diagnostics or None,
            external_condition_checks=realization.external_condition_checks,
            external_target_comparison=external_target_comparison(realization),
            diagnostic_only=(True if realization.frame_check_status == "FAIL" else None),
            eligible_for_product_write=(
                False if realization.frame_check_status == "FAIL" else None
            ),
        )
        if preview is None:
            result = IfcProductRealizationRetainedNoImageSolvedResult(**solved)
            return IfcProductRealizationPreviewAuthorization(
                result=IfcProductRealizationPreviewResult(result=result),
            )
        result = IfcProductRealizationSolvedResult(
            **solved,
            preview_mime_type=preview.mime_type,
            preview_size_bytes=preview.size_bytes,
            presentation=project_preview_presentation(preview.render_metadata),
            interface_overlays=preview.interface_overlays,
            reference_geometries=tuple(IfcGeometryPreviewReferenceResult.from_domain(item) for item in preview.reference_geometries),
            derived_only=True,
        )
        return IfcProductRealizationPreviewAuthorization(
            result=IfcProductRealizationPreviewResult(result=result),
            content=preview.content,
            title=preview.title,
        )
    if isinstance(proposal, AmbiguousProductRealizationProposal):
        return IfcProductRealizationPreviewAuthorization(
            result=IfcProductRealizationPreviewResult(
                result=IfcProductRealizationAmbiguousResult(
                    **common,
                    status="AMBIGUOUS",
                    ambiguities=proposal.ambiguities,
                )
            )
        )
    if isinstance(proposal, InfeasibleProductRealizationProposal):
        return IfcProductRealizationPreviewAuthorization(
            result=IfcProductRealizationPreviewResult(
                result=IfcProductRealizationInfeasibleResult(
                    **common,
                    status="INFEASIBLE",
                    violations=proposal.violations,
                )
            )
        )
    raise ValueError("unexpected Product Realization result branch")


def _preview_error_phase(
    code: ProductRealizationPreviewErrorCode,
) -> str:
    if code is ProductRealizationPreviewErrorCode.PREVIEW_REQUEST_INVALID:
        return "input_validation"
    if code is ProductRealizationPreviewErrorCode.RUNTIME_EXECUTION_FAILED:
        return "execution"
    if code is ProductRealizationPreviewErrorCode.ARTIFACT_INTEGRITY_FAILED:
        return "storage_verification"
    if code in {
        ProductRealizationPreviewErrorCode.RESULT_INTEGRITY_FAILED,
        ProductRealizationPreviewErrorCode.PREVIEW_AUTHORIZATION_FAILED,
    }:
        return "output_validation"
    return "read"


def _service_unavailable(label: str) -> DirectApplicationError:
    return DirectApplicationError(
        "SYSTEM_PAUSED",
        f"The {label} service is unavailable; wait for server-side recovery",
        phase="preflight",
        retryable=True,
    )


__all__ = [
    "IfcProductRealizationPreviewApplicationMixin",
    "IfcProductRealizationPreviewAuthorization",
    "ProductRealizationPreviewProviderApplication",
]
