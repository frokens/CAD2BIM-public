"""Shared Direct projection for public presentation facts in both preview entries."""

from typing import Annotated, Literal, TypeAlias

from pydantic import AfterValidator, Field, model_serializer, model_validator

from CAD2BIM_backend.domain.geometry_catalog import GeometryPreviewReference, GeometryPreviewReferenceSnapshot
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_runtime import (
    MAX_GEOMETRY_PREVIEW_PANELS,
    CanonicalGeometryFormat,
    GeometryDisplayQualityWarning,
    GeometryPreviewRenderMetadata,
    GeometryRenderCoordinateSpace,
    GeometryRenderedPreviewMetadata,
    GeometryPreviewPipesConnectionContext,
    GeometryResolvedPreviewPanel,
    GeometryStoredPreviewMetadata,
)
from CAD2BIM_mcp.direct.contract_base import DirectContract
from CAD2BIM_mcp.tool_docs.preview import (
    GEOMETRY_PREVIEW_REFERENCE_DATUM_DESCRIPTION,
    PREVIEW_DISPLAY_QUALITY_WARNING_DESCRIPTION,
)


class IfcGeometryPreviewReference(DirectContract):
    reference_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    resource_version_alias: str = Field(pattern=r"^cur\..+$", max_length=16_384)
    datum_transform: RigidTransform3D = Field(description=GEOMETRY_PREVIEW_REFERENCE_DATUM_DESCRIPTION)

    def to_domain(self) -> GeometryPreviewReference:
        return GeometryPreviewReference(reference_key=self.reference_key,
            resource_version_ref=self.resource_version_alias, datum_transform=self.datum_transform)


def _unique_references(values):
    if len({item.reference_key for item in values}) != len(values):
        raise ValueError("preview reference keys must be unique")
    return tuple(sorted(values, key=lambda item: item.reference_key))


IfcGeometryPreviewReferences = Annotated[
    tuple[IfcGeometryPreviewReference, ...], Field(max_length=8), AfterValidator(_unique_references),
]


class IfcGeometryPreviewReferenceResult(IfcGeometryPreviewReference):
    geometry_format: CanonicalGeometryFormat

    @classmethod
    def from_domain(cls, value: GeometryPreviewReferenceSnapshot):
        return cls(reference_key=value.reference_key, resource_version_alias=value.resource_version_ref,
            datum_transform=value.datum_transform,
            geometry_format=value.render_reference.geometry_format)


class IfcPreviewDisplayQualityWarningResult(DirectContract):
    __doc__ = PREVIEW_DISPLAY_QUALITY_WARNING_DESCRIPTION

    code: Literal["DISPLAY_MESH_AREA_WARNING"] = "DISPLAY_MESH_AREA_WARNING"
    message: str

    @classmethod
    def from_domain(
        cls,
        value: GeometryDisplayQualityWarning,
    ) -> "IfcPreviewDisplayQualityWarningResult":
        return cls(code=value.code, message=value.message)


class IfcPreviewPanelResult(DirectContract):
    panel_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    geometry_clipped: bool = False
    visible_overlay_ids: tuple[str, ...] = ()
    hidden_overlay_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_disjoint_overlay_disclosure(self) -> "IfcPreviewPanelResult":
        visible = set(self.visible_overlay_ids)
        hidden = set(self.hidden_overlay_ids)
        if len(visible) != len(self.visible_overlay_ids) or len(hidden) != len(
            self.hidden_overlay_ids
        ):
            raise ValueError("preview panel overlay ids must be unique")
        if visible & hidden:
            raise ValueError("preview panel overlay ids cannot be both visible and hidden")
        return self

    @classmethod
    def from_domain(cls, value: GeometryResolvedPreviewPanel) -> "IfcPreviewPanelResult":
        return cls(
            panel_key=value.panel_key,
            geometry_clipped=value.geometry_clipped,
            visible_overlay_ids=value.visible_overlay_ids,
            hidden_overlay_ids=value.hidden_overlay_ids,
        )


class IfcRenderedPreviewPresentationResult(DirectContract):
    metadata_kind: Literal["rendered"] = "rendered"
    coordinate_space: GeometryRenderCoordinateSpace
    display_quality_warnings: tuple[IfcPreviewDisplayQualityWarningResult, ...] = ()
    connection_context: GeometryPreviewPipesConnectionContext | None = None
    connection_context_diagnostics: tuple[str, ...] = ()
    panels: tuple[IfcPreviewPanelResult, ...] = Field(
        min_length=1,
        max_length=MAX_GEOMETRY_PREVIEW_PANELS,
    )

    @model_validator(mode="after")
    def require_unique_panels(self) -> "IfcRenderedPreviewPresentationResult":
        keys = tuple(panel.panel_key for panel in self.panels)
        if len(keys) != len(set(keys)):
            raise ValueError("preview panel keys must be unique")
        return self

    @model_serializer(mode="wrap")
    def omit_empty_warnings(self, handler):
        result = handler(self)
        if not self.display_quality_warnings:
            result.pop("display_quality_warnings", None)
        if self.connection_context is None:
            result.pop("connection_context", None)
        if not self.connection_context_diagnostics:
            result.pop("connection_context_diagnostics", None)
        return result

    @classmethod
    def from_domain(
        cls,
        value: GeometryRenderedPreviewMetadata,
    ) -> "IfcRenderedPreviewPresentationResult":
        return cls(
            coordinate_space=value.coordinate_space,
            display_quality_warnings=tuple(
                IfcPreviewDisplayQualityWarningResult.from_domain(item)
                for item in value.display_quality_warnings
            ),
            panels=tuple(IfcPreviewPanelResult.from_domain(item) for item in value.resolved_panels),
            connection_context=value.connection_context,
            connection_context_diagnostics=value.connection_context_diagnostics,
        )


class IfcStoredPreviewPresentationResult(DirectContract):
    metadata_kind: Literal["stored_preview"] = "stored_preview"


IfcPreviewPresentationResult: TypeAlias = Annotated[
    IfcRenderedPreviewPresentationResult | IfcStoredPreviewPresentationResult,
    Field(discriminator="metadata_kind"),
]


def project_preview_presentation(
    value: GeometryPreviewRenderMetadata | None,
) -> IfcPreviewPresentationResult | None:
    if value is None:
        return None
    if isinstance(value, GeometryRenderedPreviewMetadata):
        return IfcRenderedPreviewPresentationResult.from_domain(value)
    if isinstance(value, GeometryStoredPreviewMetadata):
        return IfcStoredPreviewPresentationResult()
    raise TypeError("unsupported preview presentation metadata")
