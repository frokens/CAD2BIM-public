"""Closed application outcome for one read-only Product Realization preview."""

from __future__ import annotations

from CAD2BIM_backend.domain.geometry_inspection import GeometryInspectionDiagnostic

from enum import StrEnum
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, model_validator

from CAD2BIM_backend.domain.geometry_catalog import (
    ProductRealizationPreviewAuthorization,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    GeometryPreview, GeometrySectionResult,
    GeometryRenderCoordinateSpace,
    GeometryRenderInterfaceMarkerKind,
    GeometryRenderInterfaceOverlay,
    GeometryRenderScene,
)


@dataclass(frozen=True, slots=True)
class FamilyStandardModelPreviewOutcome:
    """Observation of one exact formal Family asset; no realization or saved instance."""

    family_version_ref: str
    public_parameters_json: str
    preview: GeometryPreview | None = None
    section: GeometrySectionResult | None = None
    diagnostics: tuple[GeometryInspectionDiagnostic, ...] = ()
from CAD2BIM_backend.domain.product_family import ProductFamilyInterfaceKind
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationRequest,
    ProductRealizationResult,
    ProductRealizationStatus,
)


class ProductRealizationPreviewErrorCode(StrEnum):
    """Bounded, entry-neutral failure categories for the preview orchestrator."""

    FAMILY_REAUTHORIZATION_FAILED = "FAMILY_REAUTHORIZATION_FAILED"
    INPUT_LOADING_FAILED = "INPUT_LOADING_FAILED"
    RUNTIME_EXECUTION_FAILED = "RUNTIME_EXECUTION_FAILED"
    RESULT_INTEGRITY_FAILED = "RESULT_INTEGRITY_FAILED"
    ARTIFACT_INTEGRITY_FAILED = "ARTIFACT_INTEGRITY_FAILED"
    PREVIEW_AUTHORIZATION_FAILED = "PREVIEW_AUTHORIZATION_FAILED"
    PREVIEW_REQUEST_INVALID = "PREVIEW_REQUEST_INVALID"


class ProductRealizationPreviewError(RuntimeError):
    """Safe failure projection that never exposes persistent internal identities."""

    def __init__(
        self,
        code: ProductRealizationPreviewErrorCode,
        message: str,
        *,
        retryable: bool = False,
        diagnostic: dict[str, object] | None = None,
    ) -> None:
        bounded = " ".join(message.split())[:512]
        super().__init__(bounded)
        self.code = code
        self.message = bounded
        self.retryable = retryable
        self.diagnostic = diagnostic


class ProductRealizationPreviewOutcome(BaseModel):
    """Exactly one typed solve, with preview authority only for ``SOLVED``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    realization: ProductRealizationResult
    preview: ProductRealizationPreviewAuthorization | None = None
    operation_alias: str | None = None
    inspection_ref: str | None = None
    diagnostics: tuple["GeometryInspectionDiagnostic", ...] = ()

    @model_validator(mode="after")
    def require_closed_outcome(self) -> "ProductRealizationPreviewOutcome":
        solved = (self.realization.proposal.status is ProductRealizationStatus.SOLVED
                  and self.realization.execution_stage == "instantiated")
        has_preview = self.preview is not None
        render_failure = bool(self.diagnostics and not has_preview)
        if (not solved and has_preview) or (solved and not has_preview and not render_failure):
            raise ValueError(
                "only a SOLVED Product Realization can return an image preview"
            )
        return self


def product_realization_render_scene(
    request: ProductRealizationRequest,
    realization: ProductRealizationResult,
    *,
    geometry_sha256: str,
) -> GeometryRenderScene:
    """Project solved world facts into the shared renderer scene without new geometry math."""

    if realization.proposal.status is not ProductRealizationStatus.SOLVED:
        raise ValueError("only a solved Product Realization has a render scene")
    definitions = {item.interface_key: item for item in request.family_contract.interfaces}
    interface_overlays: list[GeometryRenderInterfaceOverlay] = []
    for fact in realization.interface_project_world_frames:
        definition = definitions.get(fact.interface_key)
        if definition is None:
            raise ValueError("resolved Product Realization interface is absent from its contract")
        marker_kind = (
            GeometryRenderInterfaceMarkerKind.FAMILY_INTERNAL
            if definition.interface_kind is ProductFamilyInterfaceKind.INTERNAL
            else GeometryRenderInterfaceMarkerKind.FAMILY_EXTERNAL
        )
        profile = fact.profile
        interface_overlays.append(
            GeometryRenderInterfaceOverlay(
                overlay_id=f"family:{fact.interface_key}",
                label=fact.interface_key,
                marker_kind=marker_kind,
                frame=fact.project_world_frame,
                profile=profile,
            )
        )
    return GeometryRenderScene(
        coordinate_space=GeometryRenderCoordinateSpace.PROJECT_WORLD,
        geometry_sha256=geometry_sha256,
        interface_overlays=tuple(interface_overlays),
    )


__all__ = [
    "ProductRealizationPreviewError",
    "ProductRealizationPreviewErrorCode",
    "ProductRealizationPreviewOutcome",
    "product_realization_render_scene",
]
