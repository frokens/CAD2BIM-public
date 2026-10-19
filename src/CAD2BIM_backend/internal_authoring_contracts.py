"""Closed internal authoring request contracts shared by Backend and Runner."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from CAD2BIM_backend.domain.geometry_authoring import (
    GeometryDraftWriteCommand,
)
from CAD2BIM_backend.domain.data_runtime import AuthoringPreviewSelection
from CAD2BIM_backend.domain.preview_cases import GeometryPreviewSource, FamilyPreviewSource, GeometryCasesInput, FamilyCasesInput, PreviewCaseRender
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogListQuery, GeometryPreviewReference
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_runtime import GeometryPreviewProfile
from CAD2BIM_backend.domain.geometry_inspection import (
    GeometryInspectionSelection, GeometryInspectionCoordinates,
    require_inspection_coordinate_selection,
)
from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionObservation
from CAD2BIM_backend.domain.product_family_authoring import FamilyDraftWriteCommand
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogListQuery
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationInvocation,
)
from CAD2BIM_backend.domain.text_source import SourceEdit, TextSourceView


_REFERENCE = r"^cur\..+$"
_KEY = r"^[a-z][a-z0-9_]{0,63}$"


class AuthoringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthoringPublishInput(AuthoringRequest):
    root_kind: Literal["geometry", "family"] = Field(description="Kind of the root draft to publish; Family publication includes its draft Geometry dependency closure.")
    draft_ref: str = Field(min_length=1, max_length=16_384, description="Current-scope editable root reference from Write or draft Info matching root_kind; copy unchanged.")
    expected_edit_token: int = Field(strict=True, ge=1, description="Latest root edit_token from Write or draft Info; publication rejects stale draft content atomically.")
    preview_examples: tuple[AuthoringPreviewSelection, ...] = Field(default=(), max_length=32, description="Optional saved Preview reports/cases to make available for observation and input lookup. Empty case_keys selects all report cases. Sources must match this implementation; examples do not supply formal models or gate publication on expectations.")


class GeometryListInput(AuthoringRequest):
    query: GeometryCatalogListQuery = Field(default_factory=GeometryCatalogListQuery)
    cursor: str | None = Field(default=None, min_length=1, max_length=16_384)

    @model_validator(mode="after")
    def require_cursor_without_new_filters(self):
        if self.cursor is not None and self.query != GeometryCatalogListQuery():
            raise ValueError("cursor continuation cannot change Geometry list filters")
        return self


class GeometryPublishedInfoInput(AuthoringRequest):
    target_kind: Literal["published"] = "published"
    resource_alias: str = Field(pattern=_REFERENCE, max_length=16_384)


from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftInfoTarget
GeometryInfoInput = Annotated[GeometryPublishedInfoInput | AuthoringDraftInfoTarget, Field(discriminator="target_kind")]


class GeometrySourceReadInput(AuthoringRequest):
    resource_version_alias: str = Field(pattern=_REFERENCE, max_length=16_384)
    path: str = Field(min_length=1, max_length=255)
    view: TextSourceView = Field(default_factory=lambda: TextSourceView(view_mode="full"))


GeometryWriteInput = GeometryDraftWriteCommand


class GeometrySinglePreviewInput(AuthoringRequest):
    mode: Literal["single"]
    source: GeometryPreviewSource
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    render_profile: GeometryPreviewProfile | None = None
    datum_transform: RigidTransform3D | None = None
    reference_geometries: tuple[GeometryPreviewReference, ...] = ()
    retain: bool = Field(default=False, strict=True)


class GeometryCasesPreviewInput(AuthoringRequest):
    mode: Literal["cases"]
    source: GeometryPreviewSource
    cases: GeometryCasesInput
    render: PreviewCaseRender | None = None
    datum_transform: RigidTransform3D | None = None
    reference_geometries: tuple[GeometryPreviewReference, ...] = ()
    retain: bool = Field(default=False, strict=True)


class GeometryInspectionPreviewInput(AuthoringRequest):
    mode: Literal["inspection"]
    inspection_ref: str = Field(pattern=_REFERENCE, max_length=16_384)
    selection: GeometryInspectionSelection
    coordinate_space: GeometryInspectionCoordinates
    observation: GeometryInspectionObservation

    @model_validator(mode="after")
    def exact_fragment_coordinates(self):
        require_inspection_coordinate_selection(self.selection, self.coordinate_space)
        return self


class FamilyStandardModelPreviewInput(AuthoringRequest):
    mode: Literal["family_standard_model"]
    family_version_alias: str = Field(pattern=_REFERENCE, max_length=16_384)
    observation: GeometryInspectionObservation


GeometryPreviewInput: TypeAlias = Annotated[
    GeometrySinglePreviewInput | GeometryCasesPreviewInput | GeometryInspectionPreviewInput | FamilyStandardModelPreviewInput,
    Field(discriminator="mode"),
]


class GeometryResourceVersionQueryTarget(AuthoringRequest):
    target_kind: Literal["geometry_resource_version"]
    resource_version_alias: str = Field(
        pattern=_REFERENCE, max_length=16_384,
        description="Exact successful mapped_model version copied from the visible Geometry catalog.",
    )


class GeometryInspectionQueryTarget(AuthoringRequest):
    target_kind: Literal["geometry_inspection"]
    inspection_ref: str = Field(
        pattern=_REFERENCE, max_length=16_384,
        description="Exact saved result copied from solidifying Preview or its successful Operation.",
    )
    selection: GeometryInspectionSelection
    coordinate_space: GeometryInspectionCoordinates = Field(
        description="Result or exact selected fragment_local coordinates; all distances are metres. Repair and cursor preserve this target context.",
    )

    @model_validator(mode="after")
    def exact_fragment_coordinates(self):
        require_inspection_coordinate_selection(self.selection, self.coordinate_space)
        return self


class FamilyStandardModelQueryTarget(AuthoringRequest):
    target_kind: Literal["family_standard_model"]
    family_version_alias: str = Field(
        pattern=_REFERENCE, max_length=16_384,
        description="Exact published Family version with a standard model; reads its saved local model without solving.",
    )


GeometryQueryTarget: TypeAlias = Annotated[
    GeometryResourceVersionQueryTarget | GeometryInspectionQueryTarget | FamilyStandardModelQueryTarget,
    Field(discriminator="target_kind"),
]


class GeometryQueryFreshInput(AuthoringRequest):
    mode: Literal["fresh"]
    target: GeometryQueryTarget
    code: str = Field(min_length=1, max_length=65_536)
    page_size: int = Field(default=100, ge=1, le=200)


class GeometryQueryRepairInput(AuthoringRequest):
    mode: Literal["repair"]
    parent_attempt_alias: str = Field(pattern=_REFERENCE, max_length=16_384)

    edits: tuple[SourceEdit, ...] = Field(min_length=1, max_length=16)
    page_size: int = Field(default=100, ge=1, le=200)


class GeometryQueryCursorInput(AuthoringRequest):
    mode: Literal["cursor"]
    cursor: str = Field(min_length=1, max_length=16_384)


GeometryQueryInput: TypeAlias = Annotated[
    GeometryQueryFreshInput | GeometryQueryRepairInput | GeometryQueryCursorInput,
    Field(discriminator="mode"),
]


class FamilyListInput(AuthoringRequest):
    query: ProductFamilyCatalogListQuery = Field(default_factory=ProductFamilyCatalogListQuery)
    cursor: str | None = Field(default=None, min_length=1, max_length=16_384)

    @model_validator(mode="after")
    def require_cursor_without_new_filters(self):
        if self.cursor is not None and self.query != ProductFamilyCatalogListQuery():
            raise ValueError("cursor continuation cannot change Family list filters")
        return self


class FamilyPublishedInfoInput(AuthoringRequest):
    target_kind: Literal["published"] = "published"
    family_alias: str = Field(pattern=_REFERENCE, max_length=16_384)
    before_version_no: int | None = Field(default=None, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


FamilyInfoInput = Annotated[FamilyPublishedInfoInput | AuthoringDraftInfoTarget, Field(discriminator="target_kind")]


FamilyWriteInput = FamilyDraftWriteCommand


class FamilySinglePreviewInput(AuthoringRequest):
    mode: Literal["single"]
    source: FamilyPreviewSource
    invocation: ProductRealizationInvocation
    render_profile: GeometryPreviewProfile | None = None
    reference_geometries: tuple[GeometryPreviewReference, ...] = ()
    retain: bool = Field(default=False, strict=True)


class FamilyCasesPreviewInput(AuthoringRequest):
    mode: Literal["cases"]
    source: FamilyPreviewSource
    cases: FamilyCasesInput
    render: PreviewCaseRender | None = None
    nominal_scenario_key: str | None = Field(default=None, pattern=_KEY)
    reference_geometries: tuple[GeometryPreviewReference, ...] = ()
    retain: bool = Field(default=False, strict=True)


ProductRealizationPreviewInput: TypeAlias = Annotated[
    FamilySinglePreviewInput | FamilyCasesPreviewInput, Field(discriminator="mode"),
]
